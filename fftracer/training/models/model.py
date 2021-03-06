"""Classes for FFN model definition."""

import json
import logging
import numpy as np
import math
from typing import Optional
from functools import partial

from scipy.ndimage import distance_transform_edt as distance
from scipy.special import expit

from ffn.training.model import FFNModel
from fftracer.training.self_attention.non_local import sn_non_local_block_sim
from fftracer.training.loss import make_distance_matrix, compute_ot_loss_matrix_batch, \
    compute_pixel_loss_batch, compute_alpha_batch
from fftracer.training.models.adversarial.dcgan import DCGAN
from fftracer.training.models.adversarial.patchgan import PatchGAN
import tensorflow as tf
from fftracer.utils.tensor_ops import drop_axis, add_axis, clip_gradients


def _predict_object_mask_2d(net, depth=9, self_attention_index=None):
    """
    Computes single-object mask prediction for 2d.

    Modified from ffn.training.models.convstack_3d.
    :param net: the network input; for FFN this is a concatenation of the input image
    patch and the current POM.
    :param depth: number of residual blocks to use.
    :param self_attention_index: use a self-attention block instead of a normal
    residual block at this layer, if specified.
    :return: the model logits corresponding to the updated POM.
    """
    if self_attention_index:
        assert self_attention_index <= depth
    conv = tf.contrib.layers.conv3d

    with tf.contrib.framework.arg_scope([conv], num_outputs=32,
                                        kernel_size=(3, 3, 1),
                                        padding='SAME'):
        net = conv(net, scope='conv0_a')
        net = conv(net, scope='conv0_b', activation_fn=None)
        for i in range(1, depth):
            with tf.name_scope('residual%d' % i):
                # At each iteration, net has shape [batch_size, 1, y, x, num_outputs]
                if i == self_attention_index:
                    # Use a self-attention block instead of a residual block.

                    # Self-Attention only implemented for 2D; drop the z-axis and
                    # reconstruct it after the self-attention block.
                    net = drop_axis(net, axis=1)
                    net = sn_non_local_block_sim(net, None, "self_attention")
                    net = add_axis(net, axis=1)
                else:
                    # Use a residual block.
                    in_net = net
                    net = tf.nn.relu(net)
                    net = conv(net, scope='conv%d_a' % i)
                    net = conv(net, scope='conv%d_b' % i, activation_fn=None)
                    net += in_net

    net = tf.nn.relu(net)
    logits = conv(net, 1, (1, 1, 1), activation_fn=None, scope='conv_lom')

    return logits


class FFNTracerModel(FFNModel):
    """Base class for FFN tracing models."""

    def __init__(self, deltas=[8, 8, 0], batch_size=None, dim=3,
                 fov_size=None, depth=9, loss_name="sigmoid_pixelwise", alpha=1e-6,
                 l1lambda=1e-3, self_attention_layer=None, ot_niters=10 ** 5,
                 adv_args: Optional[dict] = None):
        """

        :param deltas: [x, y, z] deltas for training and inference.
        :param batch_size: training batch size.
        :param dim: number of dimensions of model prediction (e.g. 2 = 2D input/output)
        :param fov_size: [x,y,z] fov size.
        :param depth: number of convolutional layers.
        :param loss_name: name of loss.
        :param alpha: alpha, for scheduled losses.
        :param l1lambda: l1 regularization parameter, for applicable losses.
        :param self_attention_layer: index of the layer to use a self-attention block at.
        :param adv_args: dictionary of args to be passed to adversary constructor.
        """
        try:
            fov_size = [int(x) for x in fov_size]
            alpha = float(alpha)
        except Exception as e:
            logging.error("error parsing FFNTracerModel argument: {}".format(e))

        self.dim = dim
        assert (0 < alpha < 1), "alpha must be in range (0,1)"
        super(FFNTracerModel, self).__init__(deltas, batch_size)

        self.deltas = deltas
        self.batch_size = batch_size
        self.depth = depth
        self.loss_name = loss_name
        self.alpha = alpha
        self.fov_size = fov_size
        self.l1lambda = l1lambda
        self.ot_niters = ot_niters
        self.self_attention_layer = self_attention_layer
        self.discriminator = None
        self.discriminator_loss = None
        self.adversarial_train_op = None
        # The seed is always a placeholder which is fed externally from the
        # training/inference drivers.
        self.input_seed = tf.placeholder(tf.float32, name='seed')
        self.input_patches = tf.placeholder(tf.float32, name='patches')

        # Set pred_mask_size = input_seed_size = input_image_size = fov_size and
        # also set input_seed.shape = input_patch.shape = [batch_size, z, y, x, 1] .
        self.set_uniform_io_size(fov_size)

        self._adv_args = adv_args

        self.D = None
        if self.loss_name == "ot":
            # initialize the distance matrix
            self.D = make_distance_matrix(fov_size[0])

    @property
    def adv_args(self):
        assert self._adv_args is not None, \
            "supply adversary_args to FFNTracer constructor"
        return json.loads(self._adv_args)

    def compute_sce_loss(self, logits, add_summary=True, verify_finite=True):
        """Compute the pixelwise sigmoid cross-entropy loss using logits and labels."""
        assert self.labels is not None
        assert self.loss_weights is not None
        pixel_ce_loss = tf.nn.sigmoid_cross_entropy_with_logits(logits=logits,
                                                                labels=self.labels)
        pixel_ce_loss *= self.loss_weights
        batch_ce_loss = tf.reduce_mean(pixel_ce_loss)
        if add_summary:
            tf.summary.scalar('pixel_loss', batch_ce_loss)
        if verify_finite:
            batch_ce_loss = tf.verify_tensor_all_finite(
                batch_ce_loss, 'Invalid loss detected'
            )
        return batch_ce_loss

    def alpha_weight_losses(self, loss_a, loss_b):
        """Compute alpha * loss_a + (1 - alpha) loss_b and set to self.loss.

        Computes the scheduled alpha, then apply it to compute the total weighted
        loss. The alpha scheduling this is a hockey-stick shaped decay where the
        contribution of the ce_loss bottoms out after reaching 0.01. This happens in
        (1 - 0.01)/alpha = 990,000 epochs (using a min alpha of 0.01 and alpha = 1e-6).
        """
        alpha = tf.maximum(
            1. - self.alpha * tf.cast(self.global_step, tf.float32),
            0.01
        )
        self.loss = (alpha * loss_a) + (1. - alpha) * loss_b
        tf.summary.scalar("alpha_loss", self.loss)
        self.loss = tf.verify_tensor_all_finite(self.loss, 'Invalid loss detected')

    def set_up_l1_loss(self, logits):
        """Set up l1 loss."""
        assert self.labels is not None
        assert self.loss_weights is not None

        pixel_loss = tf.abs(self.labels - logits)
        pixel_loss *= self.loss_weights
        self.loss = tf.reduce_mean(pixel_loss)
        tf.summary.scalar('l1_loss', self.loss)
        self.loss = tf.verify_tensor_all_finite(self.loss, 'Invalid loss detected')
        return

    def set_up_ssim_loss(self, logits):
        """Set up structural similarity index (SSIM) loss.

        SSIM loss does not support per-pixel weighting.
        """
        assert self.labels is not None

        ssim_loss = tf.image.ssim(self.labels, logits, max_val=1.0)

        # High values of SSIM indicate good quality, but the model will minimize loss,
        # so we reverse the sign of loss.
        ssim_loss = tf.math.negative(ssim_loss)

        batch_ssim_loss = tf.reduce_mean(ssim_loss)
        tf.summary.scalar('ssim_loss', batch_ssim_loss)

        # Compute the pixel-wise cross entropy loss
        batch_ce_loss = self.compute_sce_loss(logits, add_summary=True,
                                              verify_finite=True)

        self.alpha_weight_losses(batch_ce_loss, batch_ssim_loss)

        return

    def set_up_ms_ssim_loss(self, logits):
        """Set up multiscale structural similarity index (MS-SSIM) loss.

        MS-SSIM loss does not support per-pixel weighting.
        """
        # TODO(jpgard): try updating this to use https://github.com/andrewekhalel/sewar
        #  imlpementation of ssim instead of tf.image version; this currently leads to
        #  some kind of error.

        assert self.labels is not None

        # Compute the MS-SSIM; use a filter size of 4 because this is the largest
        # filter that can run over the data with FOV = [1,49,49] without raising an
        # error due to insufficient input size (note that default filter_size=11).

        image_loss = tf.image.ssim_multiscale(self.labels, logits, max_val=1.0,
                                              # to use original values:
                                              # power_factors=(0.0448, 0.2856, 0.3001),
                                              power_factors=[float(1) / 3] * 3,
                                              filter_size=4)

        # High values of MS-SSIM indicate good quality, but the model will minimize loss,
        # so we reverse the sign of loss.
        image_loss = tf.math.negative(image_loss)

        self.loss = tf.reduce_mean(image_loss)
        tf.summary.scalar('ms_ssim_loss', self.loss)
        self.loss = tf.verify_tensor_all_finite(self.loss, 'Invalid loss detected')
        return

    def set_up_boundary_loss(self, logits):
        """Based on 'Boundary Loss for Highly Unbalanced Segmentation', Kervadec et al.

        Code based on initial implementation at link within the official repo:
        https://github.com/LIVIAETS/surface-loss/issues/14#issuecomment-546342163
        """
        assert self.labels is not None
        assert self.loss_weights is not None
        # Compute the maximum euclidean distance for the model FOV size; this is used
        # to normalize the boundary loss and constrain it to the range (0,1) so it does
        # not dominate the loss function (otherwise boundary loss can take extreme
        # values, particularly as image size grows).
        max_dist = math.sqrt((self.fov_size[0] - 1) ** 2 +
                             (self.fov_size[1] - 1) ** 2 +
                             (self.fov_size[2] - 1) ** 2)

        def calc_dist_map(seg):
            """Calculate the distance map for a ground truth segmentation."""
            # Form a boolean mask from "soft" labels, which are set to 0.95 for FFN.
            posmask = (seg >= 0.95).astype(np.bool)
            assert posmask.any(), "ground truth must contain at least one active voxel"
            negmask = ~posmask
            res = distance(negmask) * negmask - (distance(posmask) - 1) * posmask
            res /= max_dist
            return res

        def calc_dist_map_batch(y_true):
            """Calculate the distance map for the batch."""
            return np.array([calc_dist_map(y)
                             for y in y_true]).astype(np.float32)

        # Compute the boundary loss
        y_true_dist_map = tf.py_func(func=calc_dist_map_batch,
                                     inp=[self.labels],
                                     Tout=tf.float32)
        boundary_loss = tf.math.multiply(logits, y_true_dist_map, "SurfaceLoss")
        batch_boundary_loss = tf.reduce_mean(boundary_loss)
        tf.summary.scalar('boundary_loss', batch_boundary_loss)

        # Compute the pixel-wise cross entropy loss
        batch_ce_loss = self.compute_sce_loss(logits, add_summary=True,
                                              verify_finite=True)

        self.alpha_weight_losses(batch_ce_loss, batch_boundary_loss)

    def set_up_l1_continuity_loss(self, logits):
        """Sets up the l1 continuity loss.

        L1 continuity loss uses the normal cross-entropy loss with a regularizer which
        enforces 'contnuity' between pixels.
        """
        # Compute the pixel-wise cross entropy loss
        batch_ce_loss = self.compute_sce_loss(logits, add_summary=True,
                                              verify_finite=True)
        row_wise_logits = tf.reshape(logits, [-1], 'FlattenRowWise')
        column_wise_logits = tf.reshape(tf.transpose(logits), [-1], 'FlattenColWise')
        # Compute the l1 continuity loss row-wise, subtracting each element from the
        # next element row-wise
        row_loss = row_wise_logits - tf.concat([row_wise_logits[1:], [0, ]], 0)
        row_loss = tf.abs(row_loss)
        # Compute the l1 continuity loss column-wise.
        column_loss = column_wise_logits - tf.concat([column_wise_logits[1:], [0, ]], 0)
        column_loss = tf.abs(column_loss)
        continuity_loss = row_loss + column_loss
        batch_continuity_loss = tf.reduce_mean(continuity_loss)
        tf.summary.scalar('continuity_loss', batch_continuity_loss)
        # Combine the losses to compute the total loss.
        self.loss = batch_ce_loss + self.l1lambda * batch_continuity_loss
        tf.summary.scalar('loss', self.loss)
        self.loss = tf.verify_tensor_all_finite(self.loss, 'Invalid loss detected')

    def initialize_adversary(self, logits, type):
        assert logits.get_shape().as_list() == self.labels.get_shape().as_list()
        batch_size, z, y, x, num_channels = logits.get_shape().as_list()
        if type == "dcgan":
            self.discriminator = DCGAN(input_shape=[y, x, num_channels],
                                       dim=2,
                                       **self.adv_args)
        elif type == "patchgan":
            self.discriminator = PatchGAN(input_shape=[y, x, num_channels],
                                          dim=2,
                                          **self.adv_args)
        else:
            raise NotImplementedError

    def compute_generator_loss(self, pred_fake, add_summary=True, verify_finite=True):
        """Compute generator loss using the discriminators' predictions on the generated
        data."""

        # We want the network to produce output which fools the discriminator,
        # so we use cross-entropy loss to measure how close the discriminators'
        # predictions are to an array of ONEs (which would indicate it is fooled).

        cross_entropy = tf.keras.losses.BinaryCrossentropy(from_logits=True)
        generator_loss_batch = cross_entropy(tf.ones_like(pred_fake), pred_fake)
        generator_loss = tf.reduce_mean(generator_loss_batch)
        if add_summary:
            tf.summary.scalar('adversarial_loss', generator_loss)
        if verify_finite:
            generator_loss = tf.verify_tensor_all_finite(generator_loss,
                                                         'Invalid loss detected')
        return generator_loss

    def set_up_adversarial_loss(self, logits):
        """Set up a (pure) adversarial loss."""
        self.initialize_adversary(logits, type="dcgan")

        # pred_fake and pred_true are both Tensors of shape [batch_size, 1] containing
        # the predicted probability that each element in the batch is 'real', according
        # to the discriminator.
        pred_fake = self.discriminator.predict_discriminator(logits)
        pred_true = self.discriminator.predict_discriminator(self.labels)

        generator_loss = self.compute_generator_loss(pred_fake, add_summary=True,
                                                     verify_finite=True)
        self.loss = generator_loss

        # Compute the discriminator loss
        self.discriminator.discriminator_loss(real_output=pred_true,
                                              fake_output=pred_fake)
        return

    def set_up_adversarial_plus_ce_loss(self, logits):
        """Set up adversarial + sigmoid cross-entropy loss.

        The final loss term is: L = L_adv + L_sce.
        """
        self.initialize_adversary(logits, type="dcgan")

        # pred_fake and pred_true are both Tensors of shape [batch_size, 1] containing
        # the predicted probability that each element in the batch is 'real', according
        # to the discriminator.
        pred_fake = self.discriminator.predict_discriminator(logits)
        pred_true = self.discriminator.predict_discriminator(self.labels)

        batch_generator_loss = self.compute_generator_loss(pred_fake, add_summary=True,
                                                           verify_finite=True)

        batch_sce_loss = self.compute_sce_loss(logits, add_summary=True,
                                               verify_finite=True)

        # In the final loss calculation, weight the adversarial loss by l1lambda.
        self.loss = self.l1lambda * batch_generator_loss + batch_sce_loss
        tf.summary.scalar('adversarial_plus_ce_loss', self.loss)

        # Compute the discriminator loss
        self.discriminator.discriminator_loss(real_output=pred_true,
                                              fake_output=pred_fake)

    def set_up_ot_loss(self, logits):
        """Set up the optimal transport loss."""
        # Compute the pixel loss, just for comparison
        _ = self.compute_sce_loss(logits, add_summary=True,
                                  verify_finite=True)

        # Logits has shape [batch_size, z, y, x, num_channels]
        assert logits.get_shape().as_list()[1] == 1, \
            "OT loss currently only implemented for 2D, expecting Z dimension of 1."
        logits = drop_axis(logits, axis=1, name="DropLogitsZ")
        y_true = drop_axis(self.labels, axis=1, name="DropLabelsZ")
        y_hat_probs = tf.sigmoid(logits)

        # The output of the optimal transport solver is of type float64, and its
        # precision can be important since the values may be small, so we convert
        # y_hat_probs to match this type
        y_hat_probs = tf.cast(y_hat_probs, tf.float64)

        _compute_ot_loss_matrix_batch = partial(compute_ot_loss_matrix_batch, D=self.D,
                                                ot_niters=self.ot_niters)
        _compute_pixel_loss_batch = partial(compute_pixel_loss_batch, D=self.D)

        # compute the alpha
        alpha = tf.py_func(compute_alpha_batch, [y_true, y_hat_probs], tf.float64,
                           name='ComputeAlpha')
        alpha = tf.clip_by_value(alpha, 0.1, 0.9, "ClipAlpha")

        tf.summary.histogram('ot_alpha', alpha)

        # Pi is a Tensor of shape [batch_size, d**2, d**2], where d is the square image
        # dimension. The i,j^th entry of Pi represents the cost of
        # moving a pixel i in y_hat --> pixel j in y.

        Pi = tf.py_func(_compute_ot_loss_matrix_batch, [y_true, y_hat_probs],
                        tf.float64, name='GetOTMatrix')

        delta_y_hat = tf.py_func(_compute_pixel_loss_batch, [Pi, alpha],
                                 tf.float64, name='GetOTPixelLoss')
        delta_y_hat = tf.stop_gradient(delta_y_hat)
        # drop the channels dim of y_hat_probs to compute loss
        y_hat_probs = tf.squeeze(y_hat_probs)
        pixel_loss = -tf.multiply(y_hat_probs, delta_y_hat)
        self.loss = tf.reduce_mean(pixel_loss)
        self.loss = tf.verify_tensor_all_finite(
            self.loss, 'Invalid loss detected'
        )
        tf.summary.scalar('ot_loss', self.loss)

    def set_up_patchgan_loss(self, logits):
        self.initialize_adversary(logits, type="patchgan")

        # pred_fake and pred_true are both Tensors of shape [batch_size, 1] containing
        # the predicted probability that each element in the batch is 'real', according
        # to the discriminator.
        pred_fake = self.discriminator.predict_discriminator(logits)
        pred_true = self.discriminator.predict_discriminator(self.labels)

        batch_generator_loss = self.compute_generator_loss(pred_fake, add_summary=True,
                                                           verify_finite=True)
        self.loss = batch_generator_loss
        tf.summary.scalar('patchgan_loss', self.loss)
        # Compute the discriminator loss
        self.discriminator.discriminator_loss(real_output=pred_true,
                                              fake_output=pred_fake)

    def set_up_patchgan_plus_ce_loss(self, logits):
        self.initialize_adversary(logits, type="patchgan")

        # pred_fake and pred_true are both Tensors of shape [batch_size, 1] containing
        # the predicted probability that each element in the batch is 'real', according
        # to the discriminator.
        pred_fake = self.discriminator.predict_discriminator(logits)
        pred_true = self.discriminator.predict_discriminator(self.labels)

        batch_generator_loss = self.compute_generator_loss(pred_fake, add_summary=True,
                                                           verify_finite=True)
        self.loss = batch_generator_loss
        tf.summary.scalar('patchgan_loss', self.loss)

        batch_sce_loss = self.compute_sce_loss(logits, add_summary=True,
                                               verify_finite=True)

        # In the final loss calculation, weight the adversarial loss by l1lambda.
        self.loss = self.l1lambda * batch_generator_loss + batch_sce_loss
        tf.summary.scalar('patchgan_plus_ce_loss', self.loss)

        # Compute the discriminator loss
        self.discriminator.discriminator_loss(real_output=pred_true,
                                              fake_output=pred_fake)

    def set_up_loss(self, logit_seed):
        """Set up the loss function of the model."""
        if self.loss_name == "sigmoid_pixelwise":
            self.set_up_sigmoid_pixelwise_loss(logit_seed)
        elif self.loss_name == "l1":
            self.set_up_l1_loss(logit_seed)
        elif self.loss_name == "l1_continuity":
            self.set_up_l1_continuity_loss(logit_seed)
        elif self.loss_name == "ssim":
            self.set_up_ssim_loss(logit_seed)
        elif self.loss_name == "ms_ssim":
            self.set_up_ms_ssim_loss(logit_seed)
        elif self.loss_name == "boundary":
            self.set_up_boundary_loss(logit_seed)
        elif self.loss_name == "adversarial":
            self.set_up_adversarial_loss(logit_seed)
        elif self.loss_name == "adversarial_sce":
            self.set_up_adversarial_plus_ce_loss(logit_seed)
        elif self.loss_name == "ot":
            self.set_up_ot_loss(logit_seed)
        elif self.loss_name == "patchgan":
            self.set_up_patchgan_loss(logit_seed)
        elif self.loss_name == "patchgan_sce":
            self.set_up_patchgan_plus_ce_loss(logit_seed)
        else:
            raise NotImplementedError

    def get_gradients_for_scope(self, opt, loss_op, scope, max_gradient_entry_mag=0.7):
        """Fetch the gradients in the specified scope, clipping if necessary."""
        trainable_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope)
        grads_and_vars = opt.compute_gradients(loss_op, var_list=trainable_vars)
        for g, v in grads_and_vars:
            if g is None:
                tf.logging.error('Gradient is None: %s', v.op.name)
        grads_and_vars = clip_gradients(max_gradient_entry_mag, grads_and_vars)
        for grad, var in grads_and_vars:
            # tf.summary.histogram(
            #     'gradients/%s' % var.name.replace(':0', ''), grad)
            tf.summary.histogram(var.name, grad)
        return grads_and_vars

    def set_up_optimizer(self, loss=None):
        """Sets up the training op for the model."""
        from ffn.training import optimizer
        if loss is None:
            loss = self.loss
        tf.summary.scalar('optimizer_loss', self.loss)

        ffn_opt = optimizer.optimizer_from_flags()
        ffn_grads_and_vars = self.get_gradients_for_scope(
            ffn_opt, self.loss, 'seed_update')

        if self.discriminator:
            d_opt = self.discriminator.get_optimizer()
            d_grads_and_vars = self.get_gradients_for_scope(
                d_opt, self.discriminator.d_loss, self.discriminator.d_scope_name)

        trainables = tf.trainable_variables()
        if trainables:
            for var in trainables:
                # tf.summary.histogram(var.name.replace(':0', ''), var)
                tf.summary.histogram(var.name, var)

        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            self.train_op = ffn_opt.apply_gradients(ffn_grads_and_vars,
                                                    global_step=self.global_step,
                                                    name='train')
            if self.discriminator:  # Add the adversarial train op to the FFTracer model
                self.adversarial_train_op = d_opt.apply_gradients(
                    d_grads_and_vars, global_step=self.global_step,
                    name='train_adversary')

    def define_tf_graph(self):
        """Modified for 2D from ffn.training.models.convstack_3d.ConvStack3DFFNModel ."""
        self.show_center_slice(self.input_seed)
        if self.input_patches is None:
            self.input_patches = tf.placeholder(  # [batch_size, x, y, z, num_channels]
                tf.float32, [1] + list(self.input_image_size[::-1]) + [1],
                name='patches')

        # JG: concatenate the input patches and the current mask for input to the model
        net = tf.concat([self.input_patches, self.input_seed], 4)

        with tf.variable_scope('seed_update', reuse=False):
            logit_update = _predict_object_mask_2d(
                net, self.depth, self_attention_index=self.self_attention_layer)
        logit_seed = self.update_seed(self.input_seed, logit_update)

        # Make predictions available, both as probabilities and logits.
        self.logits = logit_seed
        self.logistic = tf.sigmoid(logit_seed)

        # Create a summary histogram for the predictions; this allows for monitoring of
        # whether the predicted distribution is moving toward the desired
        # `bathtub-shaped` distribution of the ground truth.

        tf.summary.histogram("preds/sigmoid", self.logistic)

        if self.labels is not None:
            self.set_up_loss(logit_seed)
            self.set_up_optimizer()
            self.show_center_slice(logit_seed)
            self.show_center_slice(self.labels, sigmoid=False)
            self.add_summaries()

        self.saver = tf.train.Saver(keep_checkpoint_every_n_hours=1)
