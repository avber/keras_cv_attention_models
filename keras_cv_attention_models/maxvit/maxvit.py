import tensorflow as tf
from tensorflow import keras
from keras_cv_attention_models.attention_layers import (
    activation_by_name,
    batchnorm_with_activation,
    ChannelAffine,
    conv2d_no_bias,
    depthwise_conv2d_no_bias,
    drop_block,
    layer_norm,
    mhsa_with_multi_head_relative_position_embedding,
    MultiHeadRelativePositionalEmbedding,
    se_module,
    output_block,
    window_attention,
    add_pre_post_process,
)
from keras_cv_attention_models.download_and_load import reload_model_weights

PRETRAINED_DICT = {
    "maxvit_tiny": {"imagenet": {224: "e5cfd6a6bd4dea939860b6d8a29a911a"}},
    "maxvit_small": {"imagenet": {224: "6bbaff1c6316486c3ac29b607d9ebb13"}},
    "maxvit_base": {"imagenet": {224: "00c833043b87ef2861ecf79820d827e0"}},
    "maxvit_large": {"imagenet": {224: "93d079fa8171986cc272f6fb4e9b0255"}},
}


def res_MBConv(inputs, output_channel, conv_short_cut=True, strides=1, expansion=4, se_ratio=0, use_torch_mode=False, drop_rate=0, activation="gelu", name=""):
    if use_torch_mode:
        use_torch_padding, epsilon, momentum = True, 1e-5, 0.9
    else:
        use_torch_padding, epsilon, momentum = False, 0.001, 0.99

    if strides > 1:
        shortcut = keras.layers.AvgPool2D(strides, strides=strides, padding="SAME", name=name + "shortcut_pool")(inputs)
        shortcut = conv2d_no_bias(shortcut, output_channel, 1, strides=1, use_bias=True, name=name + "shortcut_") if conv_short_cut else shortcut
    else:
        shortcut = inputs

    # MBConv
    preact = batchnorm_with_activation(inputs, activation=None, zero_gamma=False, epsilon=epsilon, momentum=momentum, name=name + "preact_")
    nn = conv2d_no_bias(preact, output_channel * expansion, 1, strides=1, padding="same", name=name + "expand_")
    nn = batchnorm_with_activation(nn, activation=activation, epsilon=epsilon, momentum=momentum, name=name + "expand_")
    nn = depthwise_conv2d_no_bias(nn, 3, strides=strides, padding="SAME", use_torch_padding=use_torch_padding, name=name + "MB_")
    nn = batchnorm_with_activation(nn, activation=activation, zero_gamma=False, epsilon=epsilon, momentum=momentum, name=name + "MB_dw_")
    if se_ratio:
        nn = se_module(nn, se_ratio=se_ratio / expansion, activation="swish", name=name + "se/")
    nn = conv2d_no_bias(nn, output_channel, 1, strides=1, use_bias=True, padding="same", name=name + "MB_pw_")
    nn = drop_block(nn, drop_rate=drop_rate, name=name)
    # print(f"{shortcut.shape = }, {nn.shape = }, {strides = }")
    return keras.layers.Add(name=name + "output")([shortcut, nn])


def res_attn_ffn(inputs, output_channel, head_dimension=32, window_size=7, expansion=4, is_grid=False, drop_rate=0, layer_scale=0, activation="gelu", name=""):
    input_channel = inputs.shape[-1]
    attn = layer_norm(inputs, name=name + "attn_preact_")
    num_heads = attn.shape[-1] // head_dimension
    attention_block = lambda inputs, num_heads, name: mhsa_with_multi_head_relative_position_embedding(
        inputs, num_heads=num_heads, qkv_bias=True, out_bias=True, name=name
    )
    attn = window_attention(attn, window_size=window_size, num_heads=num_heads, is_grid=is_grid, attention_block=attention_block, name=name + "window_mhsa/")
    attn = ChannelAffine(use_bias=False, weight_init_value=layer_scale, name=name + "1_gamma")(attn) if layer_scale >= 0 else attn
    attn = drop_block(attn, drop_rate=drop_rate, name=name + "attn_")
    # print(f"{name = }, {inputs.shape = }, {shortcut.shape = }, {attn.shape = }")
    attn = keras.layers.Add(name=name + "attn_output")([inputs, attn])

    ffn = layer_norm(attn, name=name + "ffn_preact_")
    ffn = keras.layers.Dense(input_channel * expansion, name=name + "ffn/1_dense")(ffn)
    ffn = activation_by_name(ffn, activation=activation, name=name)
    ffn = keras.layers.Dense(input_channel, name=name + "ffn/2_dense")(ffn)
    ffn = ChannelAffine(use_bias=False, weight_init_value=layer_scale, name=name + "2_gamma")(ffn) if layer_scale >= 0 else ffn
    ffn = drop_block(ffn, drop_rate=drop_rate, name=name + "ffn_")
    return keras.layers.Add(name=name + "ffn_output")([attn, ffn])


def MaxViT(
    num_blocks,
    out_channels,
    stem_width=64,
    strides=[2, 2, 2, 2],
    expansion=4,
    se_ratio=0.25,
    head_dimension=32,
    window_ratio=32,
    output_filter=-1,  # -1 for out_channels[-1], 0 to disable
    use_torch_mode=False,
    layer_scale=-1,
    input_shape=(224, 224, 3),
    num_classes=1000,
    activation="gelu/app",  # means tf.nn.gelu(approximate=True)
    drop_connect_rate=0,
    classifier_activation="softmax",
    dropout=0,
    pretrained=None,
    model_name="maxvit",
    kwargs=None,
):
    inputs = keras.layers.Input(input_shape)
    if use_torch_mode:
        use_torch_padding, epsilon, momentum = True, 1e-5, 0.9
    else:
        use_torch_padding, epsilon, momentum = False, 0.001, 0.99

    """ Stem """
    nn = conv2d_no_bias(inputs, stem_width, 3, strides=2, use_bias=True, padding="same", use_torch_padding=use_torch_padding, name="stem_1_")
    nn = batchnorm_with_activation(nn, activation=activation, epsilon=epsilon, momentum=momentum, name="stem_1_")
    nn = conv2d_no_bias(nn, stem_width, 3, strides=1, use_bias=True, padding="same", use_torch_padding=use_torch_padding, name="stem_2_")
    window_size = [int(tf.math.ceil(input_shape[0] / window_ratio)), int(tf.math.ceil(input_shape[1] / window_ratio))]

    attn_ffn_common_kwargs = {
        "head_dimension": head_dimension,
        "window_size": window_size,
        "expansion": expansion,
        "layer_scale": layer_scale,
        "activation": activation,
    }

    """ Backbone [1, 2, 3, 4] """
    total_blocks = sum(num_blocks)
    global_block_id = 0
    for stack_id, (num_block, out_channel) in enumerate(zip(num_blocks, out_channels)):
        stack_se_ratio = se_ratio[stack_id] if isinstance(se_ratio, (list, tuple)) else se_ratio
        stack_strides = strides[stack_id] if isinstance(strides, (list, tuple)) else strides
        for block_id in range(num_block):
            name = "stack_{}_block_{}/".format(stack_id + 1, block_id + 1)
            stride = stack_strides if block_id == 0 else 1
            conv_short_cut = True if block_id == 0 and nn.shape[-1] != out_channel else False
            block_se_ratio = stack_se_ratio[block_id] if isinstance(stack_se_ratio, (list, tuple)) else stack_se_ratio
            block_drop_rate = drop_connect_rate * global_block_id / total_blocks
            global_block_id += 1
            nn = res_MBConv(
                nn, out_channel, conv_short_cut, stride, expansion, block_se_ratio, use_torch_mode, block_drop_rate, activation, name=name + "mbconv/"
            )
            nn = res_attn_ffn(nn, out_channel, is_grid=False, drop_rate=block_drop_rate, name=name + "block_", **attn_ffn_common_kwargs)
            nn = res_attn_ffn(nn, out_channel, is_grid=True, drop_rate=block_drop_rate, name=name + "grid_", **attn_ffn_common_kwargs)

    if num_classes > 0:
        nn = keras.layers.GlobalAveragePooling2D(name="avg_pool")(nn)
        nn = layer_norm(nn, name="post_")
        output_filter = out_channels[-1] if output_filter == -1 else output_filter
        if output_filter > 0:
            nn = keras.layers.Dense(output_filter, name="features")(nn)
            nn = activation_by_name(nn, "tanh", name="features_")
        if dropout > 0:
            nn = keras.layers.Dropout(dropout, name="head_drop")(nn)
        nn = keras.layers.Dense(num_classes, dtype="float32", activation=classifier_activation, name="predictions")(nn)

    model = keras.models.Model(inputs, nn, name=model_name)
    add_pre_post_process(model, rescale_mode="torch")
    reload_model_weights(model, PRETRAINED_DICT, "maxvit", pretrained, MultiHeadRelativePositionalEmbedding)
    return model


def MaxViT_Tiny(input_shape=(224, 224, 3), num_classes=1000, drop_connect_rate=0, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [2, 2, 5, 2]
    out_channels = [64, 128, 256, 512]
    stem_width = 64
    return MaxViT(**locals(), model_name="maxvit_tiny", **kwargs)


def MaxViT_Small(input_shape=(224, 224, 3), num_classes=1000, drop_connect_rate=0, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [2, 2, 5, 2]
    out_channels = [96, 192, 384, 768]
    stem_width = 64
    return MaxViT(**locals(), model_name="maxvit_small", **kwargs)


def MaxViT_Base(input_shape=(224, 224, 3), num_classes=1000, drop_connect_rate=0, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [2, 6, 14, 2]
    out_channels = [96, 192, 384, 768]
    stem_width = 64
    return MaxViT(**locals(), model_name="maxvit_base", **kwargs)


def MaxViT_Large(input_shape=(224, 224, 3), num_classes=1000, drop_connect_rate=0, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [2, 6, 14, 2]
    out_channels = [128, 256, 512, 1024]
    stem_width = 128
    return MaxViT(**locals(), model_name="maxvit_large", **kwargs)


def MaxViT_XLarge(input_shape=(224, 224, 3), num_classes=1000, drop_connect_rate=0, classifier_activation="softmax", pretrained="imagenet", **kwargs):
    num_blocks = [2, 6, 14, 2]
    out_channels = [192, 384, 768, 1536]
    stem_width = 192
    return MaxViT(**locals(), model_name="maxvit_xlarge", **kwargs)
