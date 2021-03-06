import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
import numpy as np
import os
import gc
from conv_lstm import ConvLSTM

# Down convolution layer
class Down_Layer(nn.Sequential):
    def __init__(self, ch_in, ch_out):
        super(Down_Layer, self).__init__()
        self.layer = self.define_layer( ch_in, ch_out )

    def define_layer(self, ch_in, ch_out):
        use_bias = True

        model = []
        model += [  nn.Conv2d( ch_in, ch_out, kernel_size=3, padding=1, bias=use_bias),
                    nn.ReLU(True),
                    nn.Conv2d( ch_out, ch_out, kernel_size=3, padding=1, bias=use_bias),
                    nn.ReLU(True) ]

        return nn.Sequential(*model)

    def forward(self, x):
        return self.layer(x)

# Up convolution layer
# input x and res_x
# upsamle(x) -> reduce_demention -> concatenate x and res_x -> up_conv_layer
class Up_Layer(nn.Sequential):
    def __init__(self, ch_in, ch_out):
        super(Up_Layer, self).__init__()
        self.ch_in = ch_in
        self.ch_out = ch_out
        self.layer = self.define_layer( )

        self.upsample = nn.UpsamplingBilinear2d(scale_factor=2)
        # add 0 padding on right and down to keep shape the same
        self.pad = nn.ConstantPad2d( (0, 1, 0, 1), 0 )
        self.degradation = nn.Conv2d( self.ch_in, self.ch_out, kernel_size=2 )

    def define_layer(self):
        use_bias = True
        pad = nn.ConstantPad2d( (0, 1, 0, 1), 0 )

        model = []
        model += [  nn.Conv2d( self.ch_in, self.ch_out, kernel_size=3, padding=1, bias=use_bias),
                    nn.ReLU(True),
                    nn.Conv2d( self.ch_out, self.ch_out, kernel_size=3, padding=1, bias=use_bias),
                    nn.ReLU(True) ]

        return nn.Sequential(*model)

    def forward(self, x, resx):
        output = self.degradation( self.pad( self.upsample(x) ) )
        output = torch.cat((output, resx), dim = 1)
        output = self.layer(output)
        return output

class recurrent_network(nn.Sequential):
    def __init__(self, fraction_index = 1):
        cuda_gpu = torch.cuda.is_available()
        self.resize_fraction = fraction_index
        super(recurrent_network, self).__init__()
        self.rnn = nn.LSTM(int(16/fraction_index), int(16/fraction_index) )
        if cuda_gpu:
            self.hidden1 = torch.zeros(1, int(16/self.resize_fraction), int(16/self.resize_fraction)).cuda()
            self.hidden2 = torch.zeros(1, int(16/self.resize_fraction), int(16/self.resize_fraction)).cuda()
        else:
            self.hidden1 = torch.zeros(1, int(16/self.resize_fraction), int(16/self.resize_fraction))
            self.hidden2 = torch.zeros(1, int(16/self.resize_fraction), int(16/self.resize_fraction))

    def forward(self, x):
        for i in x:
        # Step through the sequence one element at a time.
        # after each step, hidden contains the hidden state.
            out, (self.hidden1, self.hidden2) = self.rnn(i, (self.hidden1, self.hidden2) )

        return out

class recurrent_network_layer(nn.Sequential):
    def __init__(self, fraction_index = 1):
        super(recurrent_network_layer, self).__init__()
        cuda_gpu = torch.cuda.is_available()
        self.rnn = nn.LSTM(int(16/fraction_index), int(16/fraction_index) )
        self.resize_fraction = fraction_index
        self.free_mem_counter = 0
        if cuda_gpu:
            self.hidden1 = torch.zeros(1, int(16/self.resize_fraction), int(16/self.resize_fraction)).cuda()
            self.hidden2 = torch.zeros(1, int(16/self.resize_fraction), int(16/self.resize_fraction)).cuda()
        else:
            self.hidden1 = torch.zeros(1, int(16/self.resize_fraction), int(16/self.resize_fraction))
            self.hidden2 = torch.zeros(1, int(16/self.resize_fraction), int(16/self.resize_fraction))
        self.output_buffer = []

    def forward(self, x):
        self.init_buffer()
        for i in x:
        # Step through the sequence one element at a time.
        # after each step, hidden contains the hidden state.
            out, (self.hidden1, self.hidden2) = self.rnn(i, (self.hidden1, self.hidden2) )
            self.output_buffer.append(out)
            out.clone()
            del out
        
        return self.output_buffer
    
    def init_buffer(self):
        if len(self.output_buffer) > 0:
            self.output_buffer = []
    
class unet(nn.Module):
    def __init__(self, tot_frame_num = 100, step_ = 6, predict_ = 3 ,Gary_Scale = False, size_index = 256):
        print("gray scale:", Gary_Scale)
        super( unet, self ).__init__()
        if size_index != 256:
            self.resize_fraction = window_size = 256/size_index
        else:
            self.resize_fraction = 1

        cuda_gpu = torch.cuda.is_available()

        self.latent_feature = 0
        self.lstm_buf = []
        self.step = step_
        self.pred = predict_
        self.free_mem_counter = 0
        self.max_pool = nn.MaxPool2d(2)
        self.upsample = nn.UpsamplingBilinear2d(scale_factor=2)
        self.one_conv1 = nn.Conv2d( 1024, 512, kernel_size=1, bias=True)
        self.one_conv2 = nn.Conv2d( 1024, 512, kernel_size=1, bias=True)
        self.one_conv3 = nn.Conv2d( 512, 1024, kernel_size=1, bias=True)

        self.convlstm = ConvLSTM(input_channels=512, hidden_channels=[512, 512, 512], kernel_size=3, step=3,
                        effective_step=[2])

        
        self.one_conv4 = nn.Conv2d( 512, 384, kernel_size=1, bias=True)
        self.one_conv5 = nn.Conv2d( 256, 224, kernel_size=1, bias=True)
        self.one_conv6 = nn.Conv2d( 128, 120, kernel_size=1, bias=True)
        self.one_conv7 = nn.Conv2d( 64, 62, kernel_size=1, bias=True)
        
        self.rnn = recurrent_network_layer( fraction_index = 2 )
        self.rnn2 = recurrent_network( fraction_index = 2 )

        if Gary_Scale == True:
            self.down1 = Down_Layer(1, 64)
        else:
            self.down1 = Down_Layer( 3, 64 )

        self.down2 = Down_Layer( 64, 128 )
        self.down3 = Down_Layer( 128, 256 )
        self.down4 = Down_Layer( 256, 512 )
        self.down5 = Down_Layer( 512, 512 )

        self.up1 = Up_Layer(1024, 512)
        self.up2 = Up_Layer(512, 256)
        self.up3 = Up_Layer(256, 128)
        self.up4 = Up_Layer(128, 64)
        if Gary_Scale == True:
            self.up5 = nn.Conv2d( 64, 1, kernel_size = 1 )
        else:
            self.up5 = nn.Conv2d( 64, 3, kernel_size = 1 )
    
    def forward(self, x, free_token, test_model = False):
        self.free_token = free_token
        if ( self.free_token == True ):
            self.free_memory()

        # pop oldest buffer
        if( len(self.lstm_buf) >= self.step):   
            self.lstm_buf = self.lstm_buf[1:]
    
        # down convolution
        x1 = self.down1(x)
        x2 = self.max_pool(x1)
        
        x2 = self.down2(x2)
        x3 = self.max_pool(x2)
        
        x3 = self.down3(x3)
        x4 = self.max_pool(x3)

        x4 = self.down4(x4)
        x5 = self.max_pool(x4)
        
        x5 = self.down5(x5)

        latent_feature = x5.view(1, -1, int(16/self.resize_fraction), int(16/self.resize_fraction) )
        # add latest buffer
        # self.lstm_buf.append(latent_feature )
        if( test_model == True ):
            return latent_feature

        lstm_output =  Variable(self.convlstm(latent_feature)[0])
        
        if 'lstm_output' in locals():
            x5 = torch.cat((x5, lstm_output), dim = 1) 
            h = lstm_output.view(1, -1, x4.shape[2], x4.shape[3]) 
            x4 = self.one_conv4(x4)
            x4 = torch.cat((x4, h), dim = 1) 
            x = self.up1( x5, x4 )

            h = lstm_output.view(1, -1, x3.shape[2], x3.shape[3]) 
            x3 = self.one_conv5(x3)
            x3 = torch.cat((x3, h), dim = 1) 
            x = self.up2( x, x3 )

            h = lstm_output.view(1, -1, x2.shape[2], x2.shape[3]) 
            x2 = self.one_conv6(x2)
            x2 = torch.cat((x2, h), dim = 1) 
            x = self.up3( x, x2 )

            h = lstm_output.view(1, -1, x1.shape[2], x1.shape[3]) 
            x1 = self.one_conv7(x1)
            x1 = torch.cat((x1, h), dim = 1) 
            x = self.up4( x, x1 )
            x = F.relu(self.up5( x ))

        return x

    def free_memory(self):
        '''
        self.rnn.hidden1 = self.rnn.hidden1.detach()
        self.rnn.hidden2 = self.rnn.hidden2.detach()
        self.rnn2.hidden1 = self.rnn2.hidden1.detach()
        self.rnn2.hidden2 = self.rnn2.hidden2.detach()
        #self.convlstm.hidden_channels = self.convlstm.hidden_channels.detach()
        '''
        self.free_mem_counter = 0
