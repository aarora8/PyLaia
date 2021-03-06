from __future__ import absolute_import
from __future__ import division

import math
import operator
import os
from collections import OrderedDict
from functools import reduce

import torch
import torch.nn.functional as F
from laia.data.padding_collater import PaddedTensor
from laia.hooks import action
from laia.nn.adaptive_avgpool_2d import AdaptiveAvgPool2d
from laia.nn.image_to_sequence import ImageToSequence
from laia.nn.temporal_pyramid_maxpool_2d import TemporalPyramidMaxPool2d
from torch.nn.utils.rnn import PackedSequence, pack_padded_sequence


def build_conv_model():
    model = torch.nn.Sequential(OrderedDict([
        # conv1_1
        ('conv1_1', torch.nn.Conv2d(1, 64, kernel_size=3, padding=1)),
        ('relu1_1', torch.nn.ReLU(inplace=True)),
        # conv1_2
        ('conv1_2', torch.nn.Conv2d(64, 64, kernel_size=3, padding=1)),
        ('relu1_2', torch.nn.ReLU(inplace=True)),
        ('maxpool1', torch.nn.MaxPool2d(2, ceil_mode=True)),
        # conv2_1
        ('conv2_1', torch.nn.Conv2d(64, 128, kernel_size=3, padding=1)),
        ('relu2_1', torch.nn.ReLU(inplace=True)),
        # conv2_2
        ('conv2_2', torch.nn.Conv2d(128, 128, kernel_size=3, padding=1)),
        ('relu2_2', torch.nn.ReLU(inplace=True)),
        ('maxpool2', torch.nn.MaxPool2d(2, ceil_mode=True)),
        # conv3_1
        ('conv3_1', torch.nn.Conv2d(128, 256, kernel_size=3, padding=1)),
        ('relu3_1', torch.nn.ReLU(inplace=True)),
        # conv3_2
        ('conv3_2', torch.nn.Conv2d(256, 256, kernel_size=3, padding=1)),
        ('relu3_2', torch.nn.ReLU(inplace=True)),
        # conv3_3
        ('conv3_3', torch.nn.Conv2d(256, 256, kernel_size=3, padding=1)),
        ('relu3_3', torch.nn.ReLU(inplace=True)),
        # conv3_4
        ('conv3_4', torch.nn.Conv2d(256, 256, kernel_size=3, padding=1)),
        ('relu3_4', torch.nn.ReLU(inplace=True)),
        # conv3_5
        ('conv3_5', torch.nn.Conv2d(256, 256, kernel_size=3, padding=1)),
        ('relu3_5', torch.nn.ReLU(inplace=True)),
        # conv3_6
        ('conv3_6', torch.nn.Conv2d(256, 256, kernel_size=3, padding=1)),
        ('relu3_6', torch.nn.ReLU(inplace=True)),
        # conv4_1
        ('conv4_1', torch.nn.Conv2d(256, 512, kernel_size=3, padding=1)),
        ('relu4_1', torch.nn.ReLU(inplace=True)),
        # conv4_2
        ('conv4_2', torch.nn.Conv2d(512, 512, kernel_size=3, padding=1)),
        ('relu4_2', torch.nn.ReLU(inplace=True)),
        # conv4_3
        ('conv4_3', torch.nn.Conv2d(512, 512, kernel_size=3, padding=1)),
        ('relu4_3', torch.nn.ReLU(inplace=True))]))
    return model


def build_dortmund_model(phoc_size, levels=5):
    model = build_conv_model()

    for key, layer in OrderedDict([
        # TPP layer
        ('tpp5', TemporalPyramidMaxPool2d(levels=levels)),
        # Linear layers
        ('fc6', torch.nn.Linear(512 * sum(range(1, levels + 1)), 4096)),
        ('relu6', torch.nn.ReLU(inplace=True)),
        ('drop6', torch.nn.Dropout()),
        ('fc7', torch.nn.Linear(4096, 4096)),
        ('relu7', torch.nn.ReLU(inplace=True)),
        ('drop7', torch.nn.Dropout()),
        ('fc8', torch.nn.Linear(4096, phoc_size)),
    ]).items():
        model.add_module(key, layer)

    # Initialize parameters as Caffe does
    for name, param in model.named_parameters():
        if name[-5:] == '.bias':
            # Initialize bias to 0
            param.data.fill_(0)
        else:
            # compute fan in
            fan_in = reduce(operator.mul, param.data.size()[1:])
            param.data.normal_(mean=0, std=math.sqrt(2.0 / fan_in))

    return model


class RNNWrapper(torch.nn.Module):
    def __init__(self, module):
        super(RNNWrapper, self).__init__()
        self._module = module

    def forward(self, input):
        if isinstance(input, tuple):
            input = input[0]
        return self._module(input)

    def __repr__(self):
        return repr(self._module)


class PackedSequenceWrapper(torch.nn.Module):
    def __init__(self, module):
        super(PackedSequenceWrapper, self).__init__()
        self._module = module

    def forward(self, input):
        if isinstance(input, PackedSequence):
            x, xs = input.data, input.batch_sizes
        else:
            x, xs = input, None

        y = self._module(x)
        if xs is None:
            return pack_padded_sequence(input=y, lengths=[y.size(0)])
        else:
            return PackedSequence(data=y, batch_sizes=xs)

    def __repr__(self):
        return repr(self._module)


class DortmundCTCModule(torch.nn.Module):
    def __init__(self, num_outputs, adaptive_pool_height=16,
                 lstm_hidden_size=128, lstm_num_layers=1):
        super(DortmundCTCModule, self).__init__()
        self.conv = build_conv_model()
        self.adap_pool = AdaptiveAvgPool2d(output_size=(adaptive_pool_height, None))
        self.collapse = ImageToSequence(return_packed=True)
        self.blstm = torch.nn.LSTM(input_size=512 * adaptive_pool_height,
                                   hidden_size=lstm_hidden_size,
                                   num_layers=lstm_num_layers,
                                   dropout=0.5,
                                   bidirectional=True)
        self.linear = torch.nn.Linear(2 * lstm_hidden_size, num_outputs)

    def dropout(self, x, p=0.5):
        if isinstance(x, PackedSequence):
            return PackedSequence(data=F.dropout(x.data, p, self.training),
                                  batch_sizes=x.batch_sizes)
        elif isinstance(x, PaddedTensor):
            return PaddedTensor(data=F.dropout(x.data, p, self.training),
                                sizes=x.sizes)
        else:
            return F.dropout(x, p, self.training)

    def size_after_conv(self, xs):
        xs = xs.float()
        xs = torch.ceil((xs - 2) / 2.0 + 1)
        xs = torch.ceil((xs - 2) / 2.0 + 1)
        return xs.long()

    def forward(self, x):
        x, xs = (x.data, x.sizes) if isinstance(x, PaddedTensor) else (x, None)
        x = self.conv(x)
        if xs is not None:
            xs = self.size_after_conv(xs)
        x = self.adap_pool(x if xs is None else PaddedTensor(data=x, sizes=xs))
        x = self.collapse(x)
        x = self.dropout(x)
        x, _ = self.blstm(x)
        x = self.dropout(x)
        if isinstance(x, PackedSequence):
            return PackedSequence(data=self.linear(x.data),
                                  batch_sizes=x.batch_sizes)
        else:
            return self.linear(x)


def build_ctc_model(num_outputs,
                    adaptive_pool_height=16,
                    lstm_hidden_size=128,
                    lstm_num_layers=1):
    m = build_conv_model()
    m.add_module('adap_pool', AdaptiveAvgPool2d(
        output_size=(adaptive_pool_height, None)))
    m.add_module('collapse', ImageToSequence(return_packed=True))
    m.add_module('dropout_blstm', RNNWrapper(torch.nn.Dropout()))
    # 512 = number of filters in the last layer of Dortmund's model
    m.add_module('blstm', torch.nn.LSTM(
        input_size=512 * adaptive_pool_height,
        hidden_size=lstm_hidden_size,
        num_layers=lstm_num_layers,
        dropout=0.5,
        bidirectional=True))
    m.add_module('dropout_linear', RNNWrapper(torch.nn.Dropout()))
    m.add_module('linear',
                 PackedSequenceWrapper(
                     torch.nn.Linear(2 * lstm_hidden_size, num_outputs)))
    return m


def build_ctc_model2(
        cnn_num_filters, cnn_maxpool_size, adaptive_pool_height,
        lstm_hidden_size, lstm_num_layers, num_outputs):
    assert isinstance(cnn_num_filters, (list, tuple))
    assert isinstance(cnn_maxpool_size, (list, tuple))
    model = torch.nn.Sequential()
    input_channels = 1
    for i, n in enumerate(cnn_num_filters):
        model.add_module('conv%d' % i,
                         torch.nn.Conv2d(in_channels=input_channels,
                                         out_channels=n,
                                         kernel_size=3,
                                         padding=1))
        input_channels = n
        model.add_module('relu%d' % i, torch.nn.LeakyReLU(inplace=True))
        if (i < len(cnn_maxpool_size) and i < len(cnn_num_filters) - 1 and
                cnn_maxpool_size[i] > 0):
            model.add_module('max_pool%d' % i,
                             torch.nn.MaxPool2d(cnn_maxpool_size[i]))
    model.add_module('adaptive_pool',
                     AdaptiveAvgPool2d((adaptive_pool_height, None)))
    model.add_module('collapse', ImageToSequence(return_packed=True))
    model.add_module('dropout_blstm', RNNWrapper(torch.nn.Dropout()))
    # 512 = number of filters in the last layer of Dortmund's model
    model.add_module('blstm', torch.nn.LSTM(
        input_size=input_channels * adaptive_pool_height,
        hidden_size=lstm_hidden_size,
        num_layers=lstm_num_layers,
        dropout=0.5,
        bidirectional=True))
    model.add_module('dropout_linear', RNNWrapper(torch.nn.Dropout()))
    model.add_module('linear',
                     PackedSequenceWrapper(
                         torch.nn.Linear(2 * lstm_hidden_size, num_outputs)))
    return model


class ModelCheckpointKeepLastSaver(object):
    def __init__(self, model, filename, keep_last=5):
        self._model = model
        self._filename = os.path.normpath(filename)
        self._keep_last = keep_last

    @action
    def __call__(self):
        for i in range(self._keep_last - 1, 0, -1):
            older = ('{}-{}'.format(self._filename, i))
            newer = ('{}-{}'.format(self._filename, i - 1)
                     if i > 1 else self._filename)
            if os.path.isfile(newer):
                os.rename(newer, older)
        torch.save(self._model.state_dict(), self._filename)
