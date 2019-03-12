from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
import json
import logging
import sys

from .base import BaseAction
from .. import exceptions

logger = logging.getLogger(__name__)


class Exporter(object):
    def __init__(self, context):
        self.context = context

    def start(self):
        pass

    def start_stack(self, stack):
        pass

    def end_stack(self, stack):
        pass

    def write_output(self, key, value):
        pass

    def finish(self):
        pass


class JsonExporter(Exporter):
    def start(self):
        self.current_outputs = {}
        self.stacks = {}

    def start_stack(self, stack):
        self.current_outputs = {}

    def end_stack(self, stack):
        self.stacks[stack.name] = {
            "outputs": self.current_outputs,
            "fqn": stack.fqn
        }
        self.current_outputs = {}

    def write_output(self, key, value):
        self.current_outputs[key] = value

    def finish(self):
        json_data = json.dumps({'stacks': self.stacks}, indent=4)
        sys.stdout.write(json_data)
        sys.stdout.write('\n')
        sys.stdout.flush()


class PlainExporter(Exporter):
    def start(self):
        self.current_stack = None

    def start_stack(self, stack):
        self.current_stack = stack.name

    def end_stack(self, stack):
        self.current_stack = None

    def write_output(self, key, value):
        assert self.current_stack

        line = '{}.{}={}\n'.format(self.current_stack, key, value)
        sys.stdout.write(line)

    def finish(self):
        sys.stdout.flush()


class LogExporter(Exporter):
    def start(self):
        logger.info('Outputs for stacks: %s', self.context.get_fqn())

    def start_stack(self, stack):
        logger.info('%s:', stack.fqn)

    def write_output(self, key, value):
        logger.info('\t{}: {}'.format(key, value))


EXPORTER_CLASSES = {
    'json': JsonExporter,
    'log': LogExporter,
    'plain': PlainExporter
}

OUTPUT_FORMATS = list(EXPORTER_CLASSES.keys())


class Action(BaseAction):
    """Get information on CloudFormation stacks.

    Displays the outputs for the set of CloudFormation stacks.

    """

    def build_exporter(self, name):
        try:
            exporter_cls = EXPORTER_CLASSES[name]
        except KeyError:
            logger.error('Unknown output format "{}"'.format(name))
            return None

        return exporter_cls(self.context)

    def run(self, output_format='log', *args, **kwargs):
        if not self.context.get_stacks():
            logger.warn('WARNING: No stacks detected (error in config?)')
            return

        try:
            exporter = self.build_exporter(output_format)
        except Exception:
            logger.exception('Failed to create exporter instance')
            return

        exporter.start()

        stacks = sorted(self.context.get_stacks(), key=lambda s: s.fqn)
        for stack in stacks:
            provider = self.build_provider(stack)

            try:
                outputs = provider.get_outputs(stack.fqn)
            except exceptions.StackDoesNotExist:
                logger.info('Stack "%s" does not exist.' % (stack.fqn,))
                continue

            outputs = sorted(
                (output['OutputKey'], output['OutputValue'])
                for output in outputs)

            exporter.start_stack(stack)

            for key, value in outputs:
                exporter.write_output(key, value)

            exporter.end_stack(stack)

        exporter.finish()
