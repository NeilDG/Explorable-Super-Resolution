import os
from collections import OrderedDict
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
from torch.optim import lr_scheduler
import re
import models.networks as networks
from .base_model import BaseModel
from models.modules.loss import GANLoss, GradientPenaltyLoss,CreateRangeLoss
from torch.nn import Upsample
import DTE.DTEnet as DTEnet
import numpy as np
import h5py

class SRRaGANModel(BaseModel):
    def __init__(self, opt):
        super(SRRaGANModel, self).__init__(opt)
        train_opt = opt['train']
        self.log_path = opt['path']['log']
        self.noise_input = opt['network_G']['noise_input']
        self.relativistic_D = opt['network_D']['relativistic'] is None or bool(opt['network_D']['relativistic'])
        # define networks and load pretrained models
        self.DTE_net = None
        self.DTE_arch = opt['network_G']['DTE_arch']
        self.decomposed_output = self.DTE_arch and bool(opt['network_D']['decomposed_input'])
        if self.DTE_arch or (opt['is_train'] and opt['train']['DTE_exp']):
            assert self.opt['train']['pixel_domain']=='HR' or not self.DTE_arch,'Why should I use DTE_arch AND penalize MSE in the LR domain?'
            DTE_conf = DTEnet.Get_DTE_Conf(opt['scale'])
            DTE_conf.sigmoid_range_limit = bool(opt['network_G']['sigmoid_range_limit'])
            DTE_conf.input_range = np.array(opt['range'])
            DTE_conf.decomposed_output = bool(opt['network_D']['decomposed_input'])
            self.DTE_net = DTEnet.DTEnet(DTE_conf)
            if not self.DTE_arch:
                self.DTE_net.WrapArchitecture_PyTorch(only_padders=True)
        self.netG = networks.define_G(opt,DTE=self.DTE_net).to(self.device)  # G
        logs_2_keep = ['l_g_pix', 'l_g_fea', 'l_g_range', 'l_g_gan', 'l_d_real', 'l_d_fake',
                       'D_real', 'D_fake','D_logits_diff','psnr_val','D_update_ratio']
        self.log_dict = OrderedDict(zip(logs_2_keep, [[] for i in logs_2_keep]))
        self.debug = 'debug' in opt['path']['log']
        if self.is_train:
            self.netD = networks.define_D(opt,DTE=self.DTE_net).to(self.device)  # D
            self.netG.train()
            self.netD.train()
        self.load()  # load G and D if needed

        # define losses, optimizer and scheduler
        if self.is_train:
            # G pixel loss
            if train_opt['pixel_weight'] > 0 or self.debug:
                l_pix_type = train_opt['pixel_criterion']
                if l_pix_type == 'l1':
                    self.cri_pix = nn.L1Loss().to(self.device)
                elif l_pix_type == 'l2':
                    self.cri_pix = nn.MSELoss().to(self.device)
                else:
                    raise NotImplementedError('Loss type [{:s}] not recognized.'.format(l_pix_type))
                self.l_pix_w = train_opt['pixel_weight']
            else:
                print('Remove pixel loss.')
                self.cri_pix = None

            # G feature loss
            if train_opt['feature_weight'] > 0 or self.debug:
                l_fea_type = train_opt['feature_criterion']
                if l_fea_type == 'l1':
                    self.cri_fea = nn.L1Loss().to(self.device)
                elif l_fea_type == 'l2':
                    self.cri_fea = nn.MSELoss().to(self.device)
                else:
                    raise NotImplementedError('Loss type [{:s}] not recognized.'.format(l_fea_type))
                self.l_fea_w = train_opt['feature_weight']
            else:
                print('Remove feature loss.')
                self.cri_fea = None
            if self.cri_fea:  # load VGG perceptual loss
                self.netF = networks.define_F(opt, use_bn=False).to(self.device)

            # Range limiting loss:
            if train_opt['range_weight'] > 0 or self.debug:
                self.cri_range = CreateRangeLoss(opt['range'])
                self.l_range_w = train_opt['range_weight']
            else:
                print('Remove range loss.')
                self.cri_range = None

            # GD gan loss
            self.cri_gan = GANLoss(train_opt['gan_type'], 1.0, 0.0).to(self.device)
            self.l_gan_w = train_opt['gan_weight']
            # D_update_ratio and D_init_iters are for WGAN
            self.global_D_update_ratio = train_opt['D_update_ratio'] if train_opt['D_update_ratio'] is not None else 1
            self.D_init_iters = train_opt['D_init_iters'] if train_opt['D_init_iters'] else 0

            if train_opt['gan_type'] == 'wgan-gp':
                self.random_pt = torch.Tensor(1, 1, 1, 1).to(self.device)
                # gradient penalty loss
                self.cri_gp = GradientPenaltyLoss(device=self.device).to(self.device)
                self.l_gp_w = train_opt['gp_weigth']

            # optimizers
            # G
            wd_G = train_opt['weight_decay_G'] if train_opt['weight_decay_G'] else 0
            optim_params = []
            for k, v in self.netG.named_parameters():  # can optimize for a part of the model
                if v.requires_grad:
                    optim_params.append(v)
                else:
                    print('WARNING: params [{:s}] will not optimize.'.format(k))
            self.optimizer_G = torch.optim.Adam(optim_params, lr=train_opt['lr_G'], \
                weight_decay=wd_G, betas=(train_opt['beta1_G'], 0.999))
            self.optimizers.append(self.optimizer_G)
            # D
            wd_D = train_opt['weight_decay_D'] if train_opt['weight_decay_D'] else 0
            self.optimizer_D = torch.optim.Adam(self.netD.parameters(), lr=train_opt['lr_D'], \
                weight_decay=wd_D, betas=(train_opt['beta1_D'], 0.999))
            self.optimizers.append(self.optimizer_D)

            # schedulers
            if train_opt['lr_scheme'] == 'MultiStepLR':
                for optimizer in self.optimizers:
                    self.schedulers.append(lr_scheduler.MultiStepLR(optimizer, \
                        train_opt['lr_steps'], train_opt['lr_gamma']))
            else:
                raise NotImplementedError('MultiStepLR learning rate scheme is enough.')
            self.min_accumulation_steps = min([opt['train']['grad_accumulation_steps_G'],opt['train']['grad_accumulation_steps_D']])
            self.max_accumulation_steps = max([opt['train']['grad_accumulation_steps_G'],opt['train']['grad_accumulation_steps_D']])
            self.grad_accumulation_steps_G = opt['train']['grad_accumulation_steps_G']
            self.grad_accumulation_steps_D = opt['train']['grad_accumulation_steps_D']

        print('---------- Model initialized ------------------')
        self.print_network()
        print('-----------------------------------------------')

    def feed_data(self, data, need_HR=True):
        # LR
        self.var_L = data['LR'].to(self.device)
        if self.noise_input:
            if 'Z' in data.keys():
                cur_Z = data['Z']
            else:
                cur_Z = torch.normal(mean=torch.from_numpy(np.zeros(shape=[self.var_L.size(dim=0),1,1,1])).type(torch.FloatTensor),
                    std=torch.from_numpy(np.ones(shape=[self.var_L.size(dim=0),1,1,1])).type(torch.FloatTensor))
            self.var_L = torch.cat([(cur_Z*torch.ones(size=[1,1,self.var_L.size()[2],self.var_L.size()[3]])).type(self.var_L.type()),self.var_L],dim=1)
        if need_HR:  # train or val
            data['HR'] += (torch.rand_like(data['HR'])-0.5)/255 # Adding quantization noise to real images to avoid discriminating based on quantization differences between real and fake
            self.var_H = data['HR'].to(self.device)

            input_ref = data['ref'] if 'ref' in data else data['HR']
            self.var_ref = input_ref.to(self.device)
    def Convert_2_LR(self,size):
        return Upsample(size=size,mode='bilinear')
    def optimize_parameters(self, step):
        gradient_step_num = step//self.max_accumulation_steps
        first_grad_accumulation_step_G = step%self.grad_accumulation_steps_G==0
        last_grad_accumulation_step_G = step % self.grad_accumulation_steps_G == (self.grad_accumulation_steps_G-1)
        first_grad_accumulation_step_D = step%self.grad_accumulation_steps_D==0
        last_grad_accumulation_step_D = step % self.grad_accumulation_steps_D == (self.grad_accumulation_steps_D-1)

        if first_grad_accumulation_step_D:
            if self.global_D_update_ratio>0:
                self.cur_D_update_ratio = self.global_D_update_ratio
            elif len(self.log_dict['D_logits_diff'])<self.opt['train']['D_valid_Steps_4_G_update']:
                self.cur_D_update_ratio = self.opt['train']['D_valid_Steps_4_G_update']
            else:#Varying update ratio:
                log_mean_D_diff = np.log(max(1e-5,np.mean([val[1] for val in self.log_dict['D_logits_diff'][-self.opt['train']['D_valid_Steps_4_G_update']:]])))
                if log_mean_D_diff<-2:
                    self.cur_D_update_ratio = -2*int(np.ceil(log_mean_D_diff))
                else:
                    self.cur_D_update_ratio = max(1/50,np.floor(100*(log_mean_D_diff+1))/-100)
        # G
        # if generator_step:
        for p in self.netG.parameters():
            p.requires_grad = True
        # else:
        #     for p in self.netG.parameters():
        #         p.requires_grad = False
        self.fake_H = self.netG(self.var_L)
        if self.DTE_net is not None:
            if self.decomposed_output:
                self.fake_H = [self.DTE_net.HR_unpadder(self.fake_H[0]),self.DTE_net.HR_unpadder(self.fake_H[1])]
            else:
                self.fake_H = self.DTE_net.HR_unpadder(self.fake_H)
            self.var_H,self.var_ref = self.DTE_net.HR_unpadder(self.var_H),self.DTE_net.HR_unpadder(self.var_ref)

        # D
        if (gradient_step_num) % max([1,np.ceil(1/self.cur_D_update_ratio)]) == 0 and gradient_step_num > -self.D_init_iters:
            for p in self.netD.parameters():
                p.requires_grad = True
            if first_grad_accumulation_step_D:
                self.optimizer_D.zero_grad()
                self.l_d_real_grad_step,self.l_d_fake_grad_step,self.D_real_grad_step,self.D_fake_grad_step,self.D_logits_diff_grad_step = [],[],[],[],[]
            l_d_total = 0
            # pred_d_real = self.netD(torch.cat([self.fake_H[0],self.var_ref-self.fake_H[0]],1) if self.decomposed_output else self.var_ref)
            # pred_d_fake = self.netD((torch.cat(self.fake_H,1) if self.decomposed_output else self.fake_H).detach())  # detach to avoid BP to G
            pred_d_real = self.netD([self.fake_H[0],self.var_ref-self.fake_H[0]] if self.decomposed_output else self.var_ref)
            pred_d_fake = self.netD([t.detach() for t in self.fake_H] if self.decomposed_output else self.fake_H.detach())  # detach to avoid BP to G
            if self.relativistic_D:
                l_d_real = self.cri_gan(pred_d_real - torch.mean(pred_d_fake), True)
                l_d_fake = self.cri_gan(pred_d_fake - torch.mean(pred_d_real), False)
            else:
                l_d_real = 2*self.cri_gan(pred_d_real, True)#Multiplying by 2 to be consistent with the SRGAN code, where losses are summed and not averaged.
                l_d_fake = 2*self.cri_gan(pred_d_fake, False)

            l_d_total = (l_d_real + l_d_fake) / 2

            if self.opt['train']['gan_type'] == 'wgan-gp':
                batch_size = self.var_ref.size(0)
                if self.random_pt.size(0) != batch_size:
                    self.random_pt.resize_(batch_size, 1, 1, 1)
                self.random_pt.uniform_()  # Draw random interpolation points
                interp = self.random_pt * self.fake_H.detach() + (1 - self.random_pt) * self.var_ref
                interp.requires_grad = True
                interp_crit, _ = self.netD(interp)
                l_d_gp = self.l_gp_w * self.cri_gp(interp, interp_crit)  # maybe wrong in cls?
                l_d_total += l_d_gp

            l_d_total.backward(retain_graph=True)
            self.l_d_real_grad_step.append(l_d_real.item())
            self.l_d_fake_grad_step.append(l_d_fake.item())
            self.D_real_grad_step.append(torch.mean(pred_d_real.detach()).item())
            self.D_fake_grad_step.append(torch.mean(pred_d_fake.detach()).item())
            self.D_logits_diff_grad_step.append(torch.mean(pred_d_real.detach()-pred_d_fake.detach()).item())
            if last_grad_accumulation_step_D:
                self.optimizer_D.step()
                # set log
                self.log_dict['l_d_real'].append((gradient_step_num,np.mean(self.l_d_real_grad_step)))
                self.log_dict['l_d_fake'].append((gradient_step_num,np.mean(self.l_d_fake_grad_step)))

                if self.opt['train']['gan_type'] == 'wgan-gp':
                    self.log_dict['l_d_gp'].append((gradient_step_num,l_d_gp.item()))
                # D outputs
                self.log_dict['D_real'].append((gradient_step_num,np.mean(self.D_real_grad_step)))
                self.log_dict['D_fake'].append((gradient_step_num,np.mean(self.D_fake_grad_step)))
                self.log_dict['D_logits_diff'].append((gradient_step_num,np.mean(self.D_logits_diff_grad_step)))
                self.log_dict['D_update_ratio'].append((gradient_step_num,self.cur_D_update_ratio))

        # G step:
        generator_step = (gradient_step_num) % max([1,self.cur_D_update_ratio]) == 0 and gradient_step_num > self.D_init_iters
        if generator_step and self.opt['train']['D_valid_Steps_4_G_update']>0 and len(self.log_dict['D_logits_diff'])>=self.opt['train']['D_valid_Steps_4_G_update']:
            generator_step = all([val[1]>np.log(self.opt['train']['min_D_prob_ratio_4_G']) for val in self.log_dict['D_logits_diff'][-self.opt['train']['D_valid_Steps_4_G_update']:]])
        # When D batch is larger than G batch, run G iter on final D iter steps, to avoid updating G in the middle of calculating D gradients.
        generator_step = generator_step and step%self.grad_accumulation_steps_D>=self.grad_accumulation_steps_D-self.grad_accumulation_steps_G
        l_g_total = 0#torch.zeros(size=[],requires_grad=True).type(torch.cuda.FloatTensor)
        if generator_step:
            for p in self.netD.parameters():
                p.requires_grad = False
            if first_grad_accumulation_step_G:
                self.optimizer_G.zero_grad()
                self.l_g_pix_grad_step,self.l_g_fea_grad_step,self.l_g_gan_grad_step,self.l_g_range_grad_step = [],[],[],[]
            if self.cri_pix:  # pixel loss
                if 'pixel_domain' in self.opt['train'] and self.opt['train']['pixel_domain']=='LR':
                    LR_size = list(self.var_L.size()[-2:])
                    l_g_pix = self.cri_pix(self.Convert_2_LR(LR_size)(self.fake_H), self.Convert_2_LR(LR_size)(self.var_H))
                else:
                    l_g_pix = self.cri_pix((self.fake_H[0]+self.fake_H[1]) if self.decomposed_output else self.fake_H, self.var_H)
                l_g_total += self.l_pix_w * l_g_pix
            if self.cri_fea:  # feature loss
                if 'feature_domain' in self.opt['train'] and self.opt['train']['feature_domain']=='LR':
                    LR_size = list(self.var_L.size()[-2:])
                    real_fea = self.netF(self.Convert_2_LR(LR_size)(self.var_H)).detach()
                    fake_fea = self.netF(self.Convert_2_LR(LR_size)(self.fake_H))
                else:
                    real_fea = self.netF(self.var_H).detach()
                    fake_fea = self.netF((self.fake_H[0]+self.fake_H[1]) if self.decomposed_output else self.fake_H)
                l_g_fea = self.cri_fea(fake_fea, real_fea)
                l_g_total += self.l_fea_w * l_g_fea
            if self.cri_range: #range loss
                l_g_range = self.cri_range((self.fake_H[0]+self.fake_H[1]) if self.decomposed_output else self.fake_H)
                l_g_total += self.l_range_w * l_g_range
            # G gan + cls loss
            # pred_g_fake = self.netD(torch.cat(self.fake_H,1) if self.decomposed_output else self.fake_H)
            # pred_d_real = self.netD(torch.cat([self.fake_H[0],self.var_ref-self.fake_H[0]],1) if self.decomposed_output else self.var_ref).detach()
            pred_g_fake = self.netD(self.fake_H)
            pred_d_real = self.netD([self.fake_H[0],self.var_ref-self.fake_H[0]] if self.decomposed_output else self.var_ref).detach()

            if self.relativistic_D:
                l_g_gan = self.l_gan_w * (self.cri_gan(pred_d_real - torch.mean(pred_g_fake), False) +
                                          self.cri_gan(pred_g_fake - torch.mean(pred_d_real), True)) / 2
            else:
                l_g_gan = self.l_gan_w * self.cri_gan(pred_g_fake, True)

            l_g_total += l_g_gan

            l_g_total.backward()
            self.l_g_pix_grad_step.append(l_g_pix.item())
            self.l_g_fea_grad_step.append(l_g_fea.item())
            self.l_g_gan_grad_step.append(l_g_gan.item())
            self.l_g_range_grad_step.append(l_g_range.item())
            if last_grad_accumulation_step_G:
                self.optimizer_G.step()
                # set log
                if self.cri_pix:
                    self.log_dict['l_g_pix'].append((gradient_step_num,np.mean(self.l_g_pix_grad_step)))
                if self.cri_fea:
                    self.log_dict['l_g_fea'].append((gradient_step_num,np.mean(self.l_g_fea_grad_step)))
                if self.cri_range:
                    self.log_dict['l_g_range'].append((gradient_step_num,np.mean(self.l_g_range_grad_step)))
                self.log_dict['l_g_gan'].append((gradient_step_num,np.mean(self.l_g_gan_grad_step)))


        # set log
        # if step % self.global_D_update_ratio == 0 and step > self.D_init_iters:
            # G
            # if self.cri_pix:
            #     self.log_dict['l_g_pix'].append(l_g_pix.item())
            # if self.cri_fea:
            #     self.log_dict['l_g_fea'].append(l_g_fea.item())
            # if self.cri_range:
            #     self.log_dict['l_g_range'].append(l_g_range.item())
            # self.log_dict['l_g_gan'].append(l_g_gan.item())
        # D
        # self.log_dict['l_d_real'].append(l_d_real.item())
        # self.log_dict['l_d_fake'].append(l_d_fake.item())
        #
        # if self.opt['train']['gan_type'] == 'wgan-gp':
        #     self.log_dict['l_d_gp'].append(l_d_gp.item())
        # # D outputs
        # self.log_dict['D_real'].append(torch.mean(pred_d_real.detach()))
        # self.log_dict['D_fake'].append(torch.mean(pred_d_fake.detach()))
        # self.log_dict['D_logits_diff'].append(torch.mean(pred_d_real.detach()-pred_d_fake.detach()))

    def test(self):
        self.netG.eval()
        with torch.no_grad():
            self.fake_H = self.netG(self.var_L)
        self.netG.train()

    def get_current_log(self):
        dict_2_return = OrderedDict()
        for key in self.log_dict:
            if len(self.log_dict[key])>0:
                if isinstance(self.log_dict[key][-1],tuple) or len(self.log_dict[key][-1])>1:
                    dict_2_return[key] = self.log_dict[key][-1][1]
                else:
                    dict_2_return[key] = self.log_dict[key][-1]
        return dict_2_return
    def save_log(self):
        np.savez(os.path.join(self.log_path,'logs.npz'), ** self.log_dict)
    def load_log(self):
        loaded_log = np.load(os.path.join(self.log_path,'logs.npz'))
        self.log_dict = OrderedDict([val for val in zip(self.log_dict.keys(),[[] for i in self.log_dict.keys()])])
        for key in loaded_log.files:
            if key=='psnr_val':
                self.log_dict[key] = [tuple(val) for val in loaded_log[key]]
            else:
                self.log_dict[key] = list(loaded_log[key])
                if isinstance(self.log_dict[key][0][1],torch.Tensor):#Supporting old files where data was not converted from tensor - Causes slowness.
                    self.log_dict[key] = [[val[0],val[1].item()] for val in self.log_dict[key]]
    def display_log_figure(self):
        # keys_2_display = ['l_g_pix', 'l_g_fea', 'l_g_range', 'l_g_gan', 'l_d_real', 'l_d_fake', 'D_real', 'D_fake','D_logits_diff','psnr_val']
        keys_2_display = ['l_g_gan','D_logits_diff', 'psnr_val','l_g_pix','l_g_fea','l_g_range','D_update_ratio']
        PER_KEY_FIGURE = True
        legend_strings = []
        plt.figure(2)
        plt.clf()
        for key in keys_2_display:
            if key in self.log_dict.keys() and len(self.log_dict[key])>0:
                if PER_KEY_FIGURE:
                    plt.figure(1)
                    plt.clf()
                if isinstance(self.log_dict[key][0],tuple) or len(self.log_dict[key][0])==2:
                    cur_curve = [np.array([val[0] for val in self.log_dict[key]]),np.array([val[1] for val in self.log_dict[key]])]
                    self.plot_curves(cur_curve[0],cur_curve[1])
                    if isinstance(self.log_dict[key][0][1],torch.Tensor):
                        series_avg = np.mean([val[1].data.cpu().numpy() for val in self.log_dict[key]])
                    else:
                        series_avg = np.mean([val[1] for val in self.log_dict[key]])
                else:
                    raise Exception('Should always have step numbers')
                    self.plot_curves(self.log_dict[key])
                    # plt.plot(self.log_dict[key])
                    if isinstance(self.log_dict[key][0][1],torch.Tensor):
                        series_avg = np.mean([val[1].data.cpu().numpy() for val in self.log_dict[key]])
                    else:
                        series_avg = np.mean(self.log_dict[key])
                cur_legend_string = key + ' (%.2e)' % (series_avg)
                if PER_KEY_FIGURE:
                    plt.xlabel('Steps')
                    plt.legend([cur_legend_string], loc='best')
                    # legend_strings = []
                    plt.savefig(os.path.join(self.log_path, 'logs_%s.pdf' % (key)))
                    plt.figure(2)
                    if key=='psnr_val':
                        cur_legend_string = 'MSE_val' + ' (%s:%.2e)' % (key,series_avg)
                        cur_curve[1] = 255*np.exp(-cur_curve[1]/20)
                    cur_curve[1] = (cur_curve[1]-np.mean(cur_curve[1]))/np.std(cur_curve[1])
                    self.plot_curves(cur_curve[0],cur_curve[1])
                legend_strings.append(cur_legend_string)
        plt.legend(legend_strings,loc='best')
        plt.xlabel('Steps')
        plt.savefig(os.path.join(self.log_path,'logs.pdf'))
        # plt.close(general_fig)
        # plt.close(per_key_fig)


    def get_current_visuals(self, need_HR=True,entire_batch=False):
        out_dict = OrderedDict()
        if entire_batch:
            out_dict['LR'] = self.var_L.detach().float().cpu()
            out_dict['SR'] = (self.fake_H[0]+self.fake_H[1] if isinstance(self.fake_H,list) else self.fake_H).detach().float().cpu()
            if need_HR:
                out_dict['HR'] = self.var_H.detach().float().cpu()
        else:
            out_dict['LR'] = self.var_L.detach()[0].float().cpu()
            out_dict['SR'] = (self.fake_H[0]+self.fake_H[1] if isinstance(self.fake_H,list) else self.fake_H).detach()[0].float().cpu()
            if need_HR:
                out_dict['HR'] = self.var_H.detach()[0].float().cpu()
        return out_dict
    def plot_curves(self,steps,loss):
        SMOOTH_CURVES = True
        if SMOOTH_CURVES:
            smoothing_win = np.minimum(np.maximum(len(loss)/20,np.sqrt(len(loss))),1000).astype(np.int32)
            loss = np.convolve(loss,np.ones([smoothing_win])/smoothing_win,'valid')
            if steps is not None:
                steps = np.convolve(steps, np.ones([smoothing_win]) / smoothing_win,'valid')
        if steps is not None:
            plt.plot(steps,loss)
        else:
            plt.plot(loss)

    def print_network(self):
        # Generator
        s, n = self.get_network_description(self.netG)
        print('Number of parameters in G: {:,d}'.format(n))
        if self.is_train:
            message = '-------------- Generator --------------\n' + s + '\n'
            network_path = os.path.join(self.save_dir, '../', 'network.txt')
            if not self.opt['train']['resume']:
                with open(network_path, 'w') as f:
                    f.write(message)

            # Discriminator
            s, n,receptive_field = self.get_network_description(self.netD)
            print('Number of parameters in D: {:,d}. Receptive field size: {:,d}'.format(n,receptive_field))
            message = '\n\n\n-------------- Discriminator --------------\n' + s + '\n'
            if not self.opt['train']['resume']:
                with open(network_path, 'a') as f:
                    f.write(message)

            if self.cri_fea:  # F, Perceptual Network
                s, n = self.get_network_description(self.netF)
                print('Number of parameters in F: {:,d}'.format(n))
                message = '\n\n\n-------------- Perceptual Network --------------\n' + s + '\n'
                if not self.opt['train']['resume']:
                    with open(network_path, 'a') as f:
                        f.write(message)

    def load(self):
        resume_training = self.opt['is_train'] and self.opt['train']['resume']
        load_path_G = self.opt['path']['pretrain_model_G']
        if resume_training is not None and resume_training:
            model_name = [name for name in os.listdir(self.opt['path']['models']) if '_G.pth' in name]
            model_name = sorted(model_name,key=lambda x: int(re.search('(\d)+(?=_G.pth)',x).group(0)))[-1]
            print('Resuming training with model for G [{:s}] ...'.format(os.path.join(self.opt['path']['models'],model_name)))
            self.load_network(os.path.join(self.opt['path']['models'],model_name), self.netG)
            self.load_log()
            if self.opt['is_train']:
                model_name = [name for name in os.listdir(self.opt['path']['models']) if '_D.pth' in name]
                model_name = sorted(model_name, key=lambda x: int(re.search('(\d)+(?=_D.pth)', x).group(0)))[-1]
                print('Resuming training with model for D [{:s}] ...'.format(os.path.join(self.opt['path']['models'],model_name)))
                self.load_network(os.path.join(self.opt['path']['models'],model_name), self.netD)

        else:
            if load_path_G is not None:
                print('loading model for G [{:s}] ...'.format(load_path_G))
                self.load_network(load_path_G, self.netG)
            load_path_D = self.opt['path']['pretrain_model_D']
            if self.opt['is_train'] and load_path_D is not None:
                print('loading model for D [{:s}] ...'.format(load_path_D))
                self.load_network(load_path_D, self.netD)

    def save(self, iter_label):
        self.save_network(self.save_dir, self.netG, 'G', iter_label)
        self.save_network(self.save_dir, self.netD, 'D', iter_label)
