import torch
import torch.distributed as dist
from torch import nn
import fast_bottleneck

def kaiming_uniform_(tensor, a=0, mode='fan_in', nonlinearity='leaky_relu'):
    weight_tensor_nchw = tensor
    nn.init.kaiming_uniform_(weight_tensor_nchw, a=a, mode=mode, nonlinearity=nonlinearity)

class FrozenBatchNorm2d(torch.nn.Module):
    """
    BatchNorm2d where the batch statistics and the affine parameters are fixed
    """
    def __init__(self, n):
        super(FrozenBatchNorm2d, self).__init__()
        self.register_buffer("weight", torch.ones(n))
        self.register_buffer("bias", torch.zeros(n))
        self.register_buffer("running_mean", torch.zeros(n))
        self.register_buffer("running_var", torch.ones(n))

    def get_scale_bias(self, nhwc=False):
        scale = self.weight * self.running_var.rsqrt()
        bias = self.bias - self.running_mean * scale
        if nhwc:
            scale = scale.reshape(1, 1, 1, -1)
            bias = bias.reshape(1, 1, 1, -1)
        else:
            scale = scale.reshape(1, -1, 1, 1)
            bias = bias.reshape(1, -1, 1, 1)
        return scale, bias

    def forward(self, x):
        scale, bias = self.get_scale_bias()
        return x * scale + bias


@torch.jit.script
def drelu_dscale1(grad_o, output, scale1):
    relu_mask = (output>0).half()
    dx_relu = relu_mask * grad_o
    g1 = dx_relu * scale1
    return g1, dx_relu

@torch.jit.script
def drelu_dscale2(grad_o, output, scale1, scale2):
    relu_mask = (output>0).half()
    dx_relu = relu_mask * grad_o
    g1 = dx_relu * scale1
    g2 = dx_relu * scale2
    return g1, g2

class BottleneckFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, nhwc, stride_1x1, scale, bias, x, *conv):
        # TODO: clean up order of tensors
        args = [x, *conv[0:3], *scale[0:3], *bias[0:3]]
        ctx.downsample = len(conv) > 3
        if ctx.downsample:
            args.append(conv[3])
            args.append(scale[3])
            args.append(bias[3])

        # weight buffers are always in nhwc while shape can be nhwc or channels_last
        # here we pass in flag and let c++ handle it
        # alternatively, we can put all sizes into a fixed format and pass it in
        outputs = fast_bottleneck.forward(nhwc, stride_1x1, args)
        ctx.save_for_backward(*(args+outputs))
        # save relu outputs for drelu
        ctx.nhwc = nhwc
        ctx.stride_1x1 = stride_1x1
        return outputs[2]

    # backward relu is not exposed, MUL with mask used now
    # only support dgrad
    @staticmethod
    def backward(ctx, grad_o):
        outputs = ctx.saved_tensors[-3:]

        if ctx.downsample:
            grad_conv3, grad_conv4 = drelu_dscale2(grad_o, outputs[2], ctx.saved_tensors[6], ctx.saved_tensors[11])
        else:
            grad_conv3, grad_conv4 = drelu_dscale1(grad_o, outputs[2], ctx.saved_tensors[6])

        # create input vector for backward
        t_list = [*ctx.saved_tensors[0:10]]
        t_list.append(grad_conv3)
        t_list.append(grad_conv4)

        # outputs used for wgrad and generating drelu mask
        t_list.append(outputs[0])
        t_list.append(outputs[1])

        # in case there is downsample
        if ctx.downsample:
            t_list.append(ctx.saved_tensors[10])

        grads = fast_bottleneck.backward(ctx.nhwc, ctx.stride_1x1, t_list)

        return (None, None, None, None, *grads)

bottleneck_function = BottleneckFunction.apply

def conv3x3(in_planes, out_planes, stride=1, groups=1, dilation=1):
    """3x3 convolution with padding"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=dilation, groups=groups, bias=False, dilation=dilation)

def conv1x1(in_planes, out_planes, stride=1):
    """1x1 convolution"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=1, stride=stride, bias=False)

class Bottleneck(torch.nn.Module):
    # Bottleneck in torchvision places the stride for downsampling at 3x3 convolution(self.conv2)
    # while original implementation places the stride at the first 1x1 convolution(self.conv1)
    # according to "Deep residual learning for image recognition"https://arxiv.org/abs/1512.03385.
    # This variant is also known as ResNet V1.5 and improves accuracy according to
    # https://ngc.nvidia.com/catalog/model-scripts/nvidia:resnet_50_v1_5_for_pytorch.
    # here we put it at 1x1

    def __init__(self, in_channels, bottleneck_channels, out_channels, stride=1, groups=1,
                 dilation=1, norm_func=None, use_cudnn=False, explicit_nhwc=False):
        super(Bottleneck, self).__init__()
        if groups != 1:
            raise RuntimeError('Only support groups == 1')
        if dilation != 1:
            raise RuntimeError('Only support dilation == 1')
        if norm_func == None:
            norm_func = FrozenBatchNorm2d
        else:
            raise RuntimeError('Only support frozen BN now.')

        if stride != 1 or in_channels != out_channels:
            self.downsample = nn.Sequential(
                conv1x1(in_channels, out_channels, stride),
                norm_func(out_channels),
            )
        else:
            self.downsample = None

        # Both self.conv2 and self.downsample layers downsample the input when stride != 1
        self.conv1 = conv1x1(in_channels, bottleneck_channels, stride)
        self.conv2 = conv3x3(bottleneck_channels, bottleneck_channels)
        self.conv3 = conv1x1(bottleneck_channels, out_channels)
        self.relu = nn.ReLU(inplace=True)
        self.stride = stride

        self.bn1 = norm_func(bottleneck_channels)
        self.bn2 = norm_func(bottleneck_channels)
        self.bn3 = norm_func(out_channels)

        self.use_cudnn = use_cudnn

        # setup conv weights
        self.w_conv = [self.conv1.weight, self.conv2.weight, self.conv3.weight]
        if self.downsample is not None:
            self.w_conv.append(self.downsample[0].weight)

        # init weight in nchw format before possible transpose
        for w in self.w_conv:
            kaiming_uniform_(w, a=1)

        # TODO: prevent unsupported case usage
        # support cases
        #                 native      cudnn
        # normal             yes         no
        # channel_last       yes        yes
        # explicit_nhwc       no        yes
        self.explicit_nhwc = explicit_nhwc
        if self.explicit_nhwc:
            for p in self.parameters():
                with torch.no_grad():
                    p.data = p.data.permute(0,2,3,1).contiguous()
        return

    def forward(self, x):
        if self.use_cudnn:
            # calculate scale/bias from registered buffers
            # TODO: make this better
            s1, b1 = self.bn1.get_scale_bias(self.explicit_nhwc)
            s2, b2 = self.bn2.get_scale_bias(self.explicit_nhwc)
            s3, b3 = self.bn3.get_scale_bias(self.explicit_nhwc)
            w_scale = [s1, s2, s3]
            w_bias = [b1, b2, b3]
            if self.downsample is not None:
                s4, b4 = self.downsample[1].get_scale_bias(self.explicit_nhwc)
                w_scale.append(s4)
                w_bias.append(b4)

            out = bottleneck_function(self.explicit_nhwc, self.stride, w_scale, w_bias, x, *self.w_conv)
            return out

        if self.explicit_nhwc:
            raise RuntimeError('explicit nhwc with native ops is not supported.')

        # fallback to native ops
        identity = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.relu(out)

        return out


class SpatialBottleneckFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, spatial_group_size, local_rank, comm, stream1, nhwc, stride_1x1, scale, bias, x, *conv):
        # TODO: clean up order of tensors
        args = [x, *conv[0:3], *scale[0:3], *bias[0:3]]
        ctx.downsample = len(conv) > 3
        if ctx.downsample:
            args.append(conv[3])
            args.append(scale[3])
            args.append(bias[3])

        # weight buffers are always in nhwc while shape can be nhwc or channels_last
        # here we pass in flag and let c++ handle it
        # alternatively, we can put all sizes into a fixed format and pass it in
        outputs = fast_bottleneck.forward_init(nhwc, stride_1x1, args)
        fast_bottleneck.forward_out1(nhwc, stride_1x1, args, outputs)

        fast_bottleneck.forward_out2(nhwc, stride_1x1, args, outputs)
        # do halo exchange for outputs[0] (out1)
        if spatial_group_size > 1:
            out1 = outputs[0]
            N,Hs,W,C = list(out1.shape)
            padded_out1 = torch.empty((N,Hs+2,W,C),dtype=out1.dtype,device=out1.device)
            padded_out1[:,1:Hs+1,:,:].copy_(out1)
            stream1.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(stream1):
                # copy halos to send buffer
                send_halos = torch.empty((N,2,W,C),dtype=out1.dtype,device=out1.device)
                send_halos[:,:1,:,:].copy_(out1[:,:1,:,:])
                send_halos[:,1:,:,:].copy_(out1[:,Hs-1:,:,:])
                all_halos = torch.empty((N,2*spatial_group_size,W,C),dtype=out1.dtype,device=out1.device)
                all_halos = [all_halos[:,i*2:(i+1)*2,:,:] for i in range(spatial_group_size)]
                dist.all_gather(all_halos,send_halos)
                padded_out1_top_halo = padded_out1[:,:1,:,:]
                if local_rank > 0:
                    top_halo = all_halos[local_rank-1][:,1:,:,:]
                    padded_out1_top_halo.copy_(top_halo)
                    fat_top_halo = padded_out1[:,:3,:,:]
                    top_out2 = fast_bottleneck.forward_out2_halo(nhwc, fat_top_halo, args)
                else:
                    padded_out1_top_halo.zero_()
                padded_out1_btm_halo = padded_out1[:,Hs+1:,:,:]
                if local_rank < spatial_group_size-1:
                    btm_halo = all_halos[local_rank+1][:,:1,:,:]
                    padded_out1_btm_halo.copy_(btm_halo)
                    fat_btm_halo = padded_out1[:,Hs-1:,:,:]
                    btm_out2 = fast_bottleneck.forward_out2_halo(nhwc, fat_btm_halo, args)
                else:
                    padded_out1_btm_halo.zero_()
            torch.cuda.current_stream().wait_stream(stream1)
            out2 = outputs[1]
            if local_rank > 0:
                out2[:,:1,:,:].copy_(top_out2)
            if local_rank < spatial_group_size-1:
                out2[:,Hs-1:,:,:].copy_(btm_out2)
            
        fast_bottleneck.forward_rest(nhwc, stride_1x1, args, outputs)
        if spatial_group_size > 1:
            ctx.save_for_backward(*(args+outputs+[padded_out1]))
        else:
            ctx.save_for_backward(*(args+outputs))
        # save relu outputs for drelu
        ctx.nhwc = nhwc
        ctx.stride_1x1 = stride_1x1
        ctx.spatial_group_size = spatial_group_size
        ctx.local_rank = local_rank
        ctx.comm = comm
        ctx.stream1 = stream1
        return outputs[2]

    # backward relu is not exposed, MUL with mask used now
    # only support dgrad
    @staticmethod
    def backward(ctx, grad_o):
        if ctx.spatial_group_size > 1:
            outputs = ctx.saved_tensors[-4:-1]
        else:
           outputs = ctx.saved_tensors[-3:]

        if ctx.downsample:
            grad_conv3, grad_conv4 = drelu_dscale2(grad_o, outputs[2], ctx.saved_tensors[6], ctx.saved_tensors[11])
        else:
            grad_conv3, grad_conv4 = drelu_dscale1(grad_o, outputs[2], ctx.saved_tensors[6])

        # create input vector for backward
        t_list = [*ctx.saved_tensors[0:10]]
        t_list.append(grad_conv3)
        t_list.append(grad_conv4)

        # outputs used for wgrad and generating drelu mask
        t_list.append(outputs[0])
        t_list.append(outputs[1])

        # in case there is downsample
        if ctx.downsample:
            t_list.append(ctx.saved_tensors[10])

        grads = fast_bottleneck.backward_init(ctx.nhwc, ctx.stride_1x1, t_list)
        grad_out2 = fast_bottleneck.backward_grad_out2(ctx.nhwc, ctx.stride_1x1, t_list, grads)
        # do halo exchange of grad_out2 here
        fast_bottleneck.backward_rest(ctx.nhwc, ctx.stride_1x1, t_list, grads, grad_out2)

        return (None, None, None, None, None, None, None, None, *grads)

spatial_bottleneck_function = SpatialBottleneckFunction.apply

class SpatialBottleneck(torch.nn.Module):
    # Bottleneck in torchvision places the stride for downsampling at 3x3 convolution(self.conv2)
    # while original implementation places the stride at the first 1x1 convolution(self.conv1)
    # according to "Deep residual learning for image recognition"https://arxiv.org/abs/1512.03385.
    # This variant is also known as ResNet V1.5 and improves accuracy according to
    # https://ngc.nvidia.com/catalog/model-scripts/nvidia:resnet_50_v1_5_for_pytorch.
    # here we put it at 1x1

    def __init__(self, in_channels, bottleneck_channels, out_channels, stride=1, groups=1,
                 dilation=1, norm_func=None, use_cudnn=False, explicit_nhwc=False, 
                 spatial_group_size=1):
        super(SpatialBottleneck, self).__init__()
        if groups != 1:
            raise RuntimeError('Only support groups == 1')
        if dilation != 1:
            raise RuntimeError('Only support dilation == 1')
        if norm_func == None:
            norm_func = FrozenBatchNorm2d
        else:
            raise RuntimeError('Only support frozen BN now.')

        if stride != 1 or in_channels != out_channels:
            self.downsample = nn.Sequential(
                conv1x1(in_channels, out_channels, stride),
                norm_func(out_channels),
            )
        else:
            self.downsample = None

        # Both self.conv2 and self.downsample layers downsample the input when stride != 1
        self.conv1 = conv1x1(in_channels, bottleneck_channels, stride)
        self.conv2 = conv3x3(bottleneck_channels, bottleneck_channels)
        self.conv3 = conv1x1(bottleneck_channels, out_channels)
        self.relu = nn.ReLU(inplace=True)
        self.stride = stride

        self.bn1 = norm_func(bottleneck_channels)
        self.bn2 = norm_func(bottleneck_channels)
        self.bn3 = norm_func(out_channels)

        self.use_cudnn = use_cudnn

        # setup conv weights
        self.w_conv = [self.conv1.weight, self.conv2.weight, self.conv3.weight]
        if self.downsample is not None:
            self.w_conv.append(self.downsample[0].weight)

        # init weight in nchw format before possible transpose
        for w in self.w_conv:
            kaiming_uniform_(w, a=1)

        # TODO: prevent unsupported case usage
        # support cases
        #                 native      cudnn
        # normal             yes         no
        # channel_last       yes        yes
        # explicit_nhwc       no        yes
        self.explicit_nhwc = explicit_nhwc
        if self.explicit_nhwc:
            for p in self.parameters():
                with torch.no_grad():
                    p.data = p.data.permute(0,2,3,1).contiguous()

        # spatial communicator
        self.spatial_group_size = spatial_group_size
        if spatial_group_size > 1:
            world_size = dist.get_world_size()
            num_groups = world_size // spatial_group_size
            assert(num_groups*spatial_group_size == world_size), "torch.distributed.get_world_size() must be multiple of group_size"
            rank = dist.get_rank()
            self.local_rank = rank % spatial_group_size
            for group in range(num_groups):
                ranks = list(range(group*spatial_group_size,(group+1)*spatial_group_size))
                comm = torch.distributed.new_group(ranks=ranks)
                if rank in ranks:
                    self.communicator = comm
            self.stream1 = torch.cuda.Stream()
            self.spatial_args = self.spatial_group_size, self.local_rank, self.communicator, self.stream1
        else:
            self.spatial_args = 1, 0, None, None

        return

    def forward(self, x):
        if self.use_cudnn:
            # calculate scale/bias from registered buffers
            # TODO: make this better
            s1, b1 = self.bn1.get_scale_bias(self.explicit_nhwc)
            s2, b2 = self.bn2.get_scale_bias(self.explicit_nhwc)
            s3, b3 = self.bn3.get_scale_bias(self.explicit_nhwc)
            w_scale = [s1, s2, s3]
            w_bias = [b1, b2, b3]
            if self.downsample is not None:
                s4, b4 = self.downsample[1].get_scale_bias(self.explicit_nhwc)
                w_scale.append(s4)
                w_bias.append(b4)

            out = spatial_bottleneck_function(*self.spatial_args, self.explicit_nhwc, self.stride, w_scale, w_bias, x, *self.w_conv)
            return out

        if self.explicit_nhwc:
            raise RuntimeError('explicit nhwc with native ops is not supported.')

        # fallback to native ops
        identity = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.relu(out)

        return out
