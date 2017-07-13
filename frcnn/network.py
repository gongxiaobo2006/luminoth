import numpy as np
import sonnet as snt
import tensorflow as tf

from .dataset import TFRecordDataset
from .pretrained import VGG, ResNetV2
from .rcnn import RCNN
from .roi_pool import ROIPoolingLayer
from .rpn import RPN

from .utils.generate_anchors import generate_anchors as generate_anchors_reference
from .utils.ops import meshgrid
from .utils.image import draw_bboxes, normalize_bboxes
from .utils.vars import variable_summaries


PRETRAINED_MODULES = {
    'vgg': VGG,
    'resnet': ResNetV2,
    'resnetv2': ResNetV2,
}


class FasterRCNN(snt.AbstractModule):
    """Faster RCNN Network"""

    def __init__(self, config, debug=False, with_rcnn=True, num_classes=None, pretrained_type='vgg', name='fasterrcnn'):
        super(FasterRCNN, self).__init__(name=name)

        self._cfg = config
        self._num_classes = num_classes
        self._with_rcnn = with_rcnn
        self._debug = debug

        self._anchor_base_size = self._cfg.ANCHOR_BASE_SIZE
        self._anchor_scales = np.array(self._cfg.ANCHOR_SCALES)
        self._anchor_ratios = np.array(self._cfg.ANCHOR_RATIOS)
        self._anchor_stride = self._cfg.ANCHOR_STRIDE  # TODO: Value depends on use of VGG vs Resnet (?)

        self._anchor_reference = generate_anchors_reference(
            self._anchor_base_size, self._anchor_ratios, self._anchor_scales
        )
        self._num_anchors = self._anchor_reference.shape[0]

        self._rpn_cls_loss_weight = 1.0
        self._rpn_reg_loss_weight = 2.0

        self._rcnn_cls_loss_weight = 1.0
        self._rcnn_reg_loss_weight = 2.0

        if pretrained_type not in PRETRAINED_MODULES:
            raise ValueError(
                'Invalid type for pretrained module: "{}", should be one of: {}'.format(
                    pretrained_type, PRETRAINED_MODULES
                )
            )
        self._pretrained_class = PRETRAINED_MODULES[pretrained_type]

        with self._enter_variable_scope():
            self._pretrained = self._pretrained_class(trainable=self._cfg.PRETRAINED_TRAINABLE)
            self._rpn = RPN(self._num_anchors, debug=self._debug)
            if self._with_rcnn:
                self._roi_pool = ROIPoolingLayer(debug=self._debug)
                self._rcnn = RCNN(self._num_classes, debug=self._debug)

    def _build(self, image, gt_boxes, is_training=True):
        """
        Returns bounding boxes and classification probabilities.

        Args:
            image: A tensor with the image.
                Its shape should be `(1, height, width, 3)`.
            gt_boxes: A tensor with all the ground truth boxes of that image.
                Its shape should be `(num_gt_boxes, 4)`
                Where for each gt box we have (x1, y1, x2, y2), in that order.

        Returns:
            classification_prob: A tensor with the softmax probability for
                each of the bounding boxes found in the image.
                Its shape should be: (num_bboxes, num_categories + 1)
            classification_bbox: A tensor with the bounding boxes found.
                It's shape should be: (num_bboxes, 4). For each of the bboxes
                we have (x1, y1, x2, y2)
        """

        image_shape = tf.shape(image)[1:3]
        pretrained_dict = self._pretrained(image, is_training=is_training)
        pretrained_feature_map = pretrained_dict['net']

        variable_summaries(pretrained_feature_map, 'pretrained_feature_map', ['RPN'])

        all_anchors = self._generate_anchors(pretrained_feature_map)
        rpn_prediction = self._rpn(
            pretrained_feature_map, gt_boxes, image_shape, all_anchors,
            is_training=is_training
        )

        prediction_dict = {
            'rpn_prediction': rpn_prediction,
        }

        if self._debug:
            prediction_dict['image'] = image
            prediction_dict['image_shape'] = image_shape
            prediction_dict['all_anchors'] = all_anchors
            prediction_dict['gt_boxes'] = gt_boxes
            prediction_dict['pretrained_dict'] = pretrained_dict

        if self._with_rcnn:
            roi_prediction = self._roi_pool(rpn_prediction['proposals'], pretrained_feature_map, image_shape)

            # TODO: Missing mapping classification_bbox to real coordinates.
            # (and trimming, and NMS?)
            classification_prediction = self._rcnn(
                roi_prediction['roi_pool'], rpn_prediction['proposals'], gt_boxes, image_shape)

            prediction_dict['classification_prediction'] = classification_prediction
            if self._debug:
                prediction_dict['roi_prediction'] = roi_prediction


        # rpn_prediction['proposals_normalized'] = normalize_bboxes(image, rpn_prediction['proposals'])

        # tf.summary.image('image', image, max_outputs=20)
        # tf.summary.image('top_1_rpn_boxes', draw_bboxes(image, rpn_prediction['proposals'], 1), max_outputs=20)
        # tf.summary.image('top_10_rpn_boxes', draw_bboxes(image, rpn_prediction['proposals'], 10), max_outputs=20)
        # tf.summary.image('top_20_rpn_boxes', draw_bboxes(image, rpn_prediction['proposals'], 20), max_outputs=20)

        return prediction_dict

    def loss(self, prediction_dict):
        """
        Compute the joint training loss for Faster RCNN.
        """
        with self._enter_variable_scope():
            rpn_loss_dict = self._rpn.loss(
                prediction_dict['rpn_prediction']
            )

            # Losses have a weight assigned.
            rpn_loss_dict['rpn_cls_loss'] = rpn_loss_dict['rpn_cls_loss'] * self._rpn_cls_loss_weight
            rpn_loss_dict['rpn_reg_loss'] = rpn_loss_dict['rpn_reg_loss'] * self._rpn_reg_loss_weight

            prediction_dict['rpn_loss_dict'] = rpn_loss_dict

            if self._with_rcnn:
                rcnn_loss_dict = self._rcnn.loss(
                    prediction_dict['classification_prediction']
                )

                rcnn_loss_dict['rcnn_cls_loss'] = rcnn_loss_dict['rcnn_cls_loss'] * self._rcnn_cls_loss_weight
                rcnn_loss_dict['rcnn_reg_loss'] = rcnn_loss_dict['rcnn_reg_loss'] * self._rcnn_reg_loss_weight

                prediction_dict['rcnn_loss_dict'] = rcnn_loss_dict
            else:
                rcnn_loss_dict = {}

            for loss_name, loss_tensor in list(rpn_loss_dict.items()) + list(rcnn_loss_dict.items()):
                tf.summary.scalar(loss_name, loss_tensor, collections=['Losses'])
                tf.losses.add_loss(loss_tensor)

            regularization_loss = tf.losses.get_regularization_loss()
            no_reg_loss = tf.losses.get_total_loss(add_regularization_losses=False)
            total_loss = tf.losses.get_total_loss()

            tf.summary.scalar('total_loss', total_loss, collections=['Losses'])
            tf.summary.scalar('no_reg_loss', no_reg_loss, collections=['Losses'])
            tf.summary.scalar('regularization_loss', regularization_loss, collections=['Losses'])

            return total_loss

    def _generate_anchors(self, feature_map):
        with self._enter_variable_scope():
            with tf.variable_scope('generate_anchors'):
                feature_map_shape = tf.shape(feature_map)[1:3]
                grid_width = feature_map_shape[1]
                grid_height = feature_map_shape[0]
                shift_x = tf.range(grid_width) * self._anchor_stride
                shift_y = tf.range(grid_height) * self._anchor_stride
                shift_x, shift_y = meshgrid(shift_x, shift_y)

                shift_x = tf.reshape(shift_x, [-1])
                shift_y = tf.reshape(shift_y, [-1])

                shifts = tf.stack(
                    [shift_x, shift_y, shift_x, shift_y],
                    axis=0
                )

                shifts = tf.transpose(shifts)
                # Shifts now is a (H x W, 4) Tensor

                num_anchors = self._anchor_reference.shape[0]
                num_anchor_points = tf.shape(shifts)[0]

                all_anchors = (
                    self._anchor_reference.reshape((1, num_anchors, 4)) +
                    tf.transpose(tf.reshape(shifts, (1, num_anchor_points, 4)), (1, 0, 2))
                )

                all_anchors = tf.reshape(all_anchors, (num_anchors * num_anchor_points, 4))
                return all_anchors

    @property
    def summary(self):
        """
        Generate merged summary of all the sub-summaries used inside the
        Faster R-CNN network.
        """
        summaries = [
            tf.summary.merge_all(key='Losses'),
            tf.summary.merge_all(key='RPN'),
        ]

        if self._with_rcnn:
            summaries.append(tf.summary.merge_all(key='RCNN'))

        return tf.summary.merge(summaries)
