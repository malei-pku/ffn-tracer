"""Runs FFN inference within a dense bounding box.

Inference is performed within a single process.

Forked from ffn/run_inference.py
"""

import os
import time

from google.protobuf import text_format
from absl import app
from absl import flags
from tensorflow import gfile

from ffn.utils import bounding_box_pb2
from ffn.inference import inference
from ffn.inference import inference_flags

FLAGS = flags.FLAGS

flags.DEFINE_string('bounding_box', None,
                    'BoundingBox proto in text format defining the area '
                    'to segmented.')


def main(unused_argv):
  request = inference_flags.request_from_flags()

  if not gfile.Exists(request.segmentation_output_dir):
    gfile.MakeDirs(request.segmentation_output_dir)

  bbox = bounding_box_pb2.BoundingBox()
  text_format.Parse(FLAGS.bounding_box, bbox)

  runner = inference.Runner()
  runner.start(request)
  runner.run((bbox.start.z, bbox.start.y, bbox.start.x),
             (bbox.size.z, bbox.size.y, bbox.size.x))

  counter_path = os.path.join(request.segmentation_output_dir, 'counters.txt')
  if not gfile.Exists(counter_path):
    runner.counters.dump(counter_path)


if __name__ == '__main__':
  app.run(main)
