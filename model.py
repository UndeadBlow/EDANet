import tensorflow as tf
from architecture import eda_net


MOVING_AVERAGE_DECAY = 0.995
IGNORE_LABEL = 255


def model_fn(features, labels, mode, params):
    """
    This is a function for creating a computational tensorflow graph.
    The function is in format required by tf.estimator.
    """

    is_training = mode == tf.estimator.ModeKeys.TRAIN
    images = features
    logits = eda_net(
        images, is_training, k=params['k'],
        num_classes=params['num_labels']
    )
    predictions = {
        'probabilities': tf.nn.softmax(logits, axis=3),
        'labels': tf.argmax(logits, axis=3, output_type=tf.int32)
    }

    if mode == tf.estimator.ModeKeys.PREDICT:
        export_outputs = tf.estimator.export.PredictOutput({
            name: tf.identity(tensor, name)
            for name, tensor in predictions.items()
        })
        return tf.estimator.EstimatorSpec(
            mode, predictions=predictions,
            export_outputs={'outputs': export_outputs}
        )

    # add l2 regularization
    with tf.name_scope('weight_decay'):
        add_weight_decay(params['weight_decay'])
        regularization_loss = tf.losses.get_regularization_loss()
        tf.summary.scalar('regularization_loss', regularization_loss)

    with tf.name_scope('losses'):

        class_weights = tf.constant(params['class_weights'], tf.float32)
        # it has shape [num_labels]

        shape = tf.shape(logits)
        batch_size, height, width, num_labels = tf.unstack(shape, axis=0)

        labels_flat = tf.reshape(labels, [-1])
        logits = tf.reshape(logits, [batch_size * height * width, num_labels])

        not_ignore = tf.not_equal(labels_flatten, IGNORE_LABEL)
        labels_flat = tf.boolean_mask(labels_flat, not_ignore)
        logits = tf.boolean_mask(logits, not_ignore)

        weights = tf.gather(class_weights, labels_flat)
        losses = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=labels_flat, logits=logits)
        cross_entropy = tf.reduce_mean(losses * weights, axis=0)

        tf.losses.add_loss(cross_entropy)
        tf.summary.scalar('cross_entropy', cross_entropy)

    total_loss = tf.losses.get_total_loss(add_regularization_losses=True)

    with tf.name_scope('eval_metrics'):

        # this is a stupid metric actually
        accuracy = tf.reduce_mean(tf.to_float(tf.equal(predictions['labels'], labels)), axis=[0, 1, 2])

        # this is better
        mean_iou = compute_iou(predictions['labels'], labels, params['num_labels'])

    if mode == tf.estimator.ModeKeys.EVAL:
        eval_metric_ops = {
            'eval_accuracy': tf.metrics.mean(accuracy),
            'eval_mean_iou': tf.metrics.mean(mean_iou)
        }
        return tf.estimator.EstimatorSpec(
            mode, loss=total_loss,
            eval_metric_ops=eval_metric_ops
        )

    assert mode == tf.estimator.ModeKeys.TRAIN
    with tf.variable_scope('learning_rate'):
        global_step = tf.train.get_global_step()
        learning_rate = tf.train.polynomial_decay(
            params['initial_learning_rate'], global_step,
            params['decay_steps'], params['end_learning_rate'],
            power=1.0  # linear decay
        )
        tf.summary.scalar('learning_rate', learning_rate)

    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
    with tf.control_dependencies(update_ops), tf.variable_scope('optimizer'):
        optimizer = tf.train.AdamOptimizer(learning_rate)
        grads_and_vars = optimizer.compute_gradients(total_loss)
        train_op = optimizer.apply_gradients(grads_and_vars, global_step)

    for g, v in grads_and_vars:
        tf.summary.histogram(v.name[:-2] + '_hist', v)
        tf.summary.histogram(v.name[:-2] + '_grad_hist', g)

    with tf.control_dependencies([train_op]), tf.name_scope('ema'):
        ema = tf.train.ExponentialMovingAverage(decay=MOVING_AVERAGE_DECAY, num_updates=global_step)
        train_op = ema.apply(tf.trainable_variables())

    tf.summary.scalar('train_accuracy', accuracy)
    tf.summary.scalar('train_mean_iou', mean_iou)
    return tf.estimator.EstimatorSpec(mode, loss=total_loss, train_op=train_op)


def add_weight_decay(weight_decay):
    """Add L2 regularization to all (or some) trainable kernel weights."""
    kernels = [v for v in tf.trainable_variables() if 'weights' in v.name]
    for K in kernels:
        x = tf.multiply(weight_decay, tf.nn.l2_loss(K))
        tf.add_to_collection(tf.GraphKeys.REGULARIZATION_LOSSES, x)


class RestoreMovingAverageHook(tf.train.SessionRunHook):
    def __init__(self, model_dir):
        super(RestoreMovingAverageHook, self).__init__()
        self.model_dir = model_dir

    def begin(self):
        ema = tf.train.ExponentialMovingAverage(decay=MOVING_AVERAGE_DECAY)
        variables_to_restore = ema.variables_to_restore()
        self.load_ema = tf.contrib.framework.assign_from_checkpoint_fn(
            tf.train.latest_checkpoint(self.model_dir), variables_to_restore
        )

    def after_create_session(self, sess, coord):
        tf.logging.info('Loading EMA weights...')
        self.load_ema(sess)


def compute_iou(x, y, num_labels):
    """
    Arguments:
        x, y: int tensors with shape [b, h, w],
            possible values that they can contain
            are {0, 1, ..., num_labels - 1, IGNORE_LABEL}.
            Note that ignore label is ignored here.
        num_labels: an integer.
    Returns:
        a float tensor with shape [].
    """
    unique_labels = tf.range(num_labels, dtype=tf.int32)

    x = tf.equal(tf.expand_dims(x, 3), unique_labels)
    y = tf.equal(tf.expand_dims(y, 3), unique_labels)
    intersection = tf.to_float(tf.logical_and(x, y))
    union = tf.to_float(tf.logical_or(x, y))
    # they all have shape [b, h, w, num_labels]

    intersection = tf.reduce_sum(intersection, axis=[1, 2])
    union = tf.reduce_sum(union, axis=[1, 2])
    union = tf.maximum(union, 1.0)
    # they have shape [b, num_labels]

    return tf.reduce_mean(intersection/union, axis=[0, 1])
