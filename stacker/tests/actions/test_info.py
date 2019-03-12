from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
import json
import unittest

from mock import Mock, patch
from six import StringIO
from testfixtures import LogCapture

from stacker.actions.info import Action
from stacker.tests.actions.test_build import TestProvider
from stacker.tests.factories import mock_context, MockProviderBuilder


def mock_stack(name, fqn, **kwargs):
    m = Mock(fqn=fqn, **kwargs)
    m.name = name
    return m


class TestInfoAction(unittest.TestCase):
    def _set_up_stacks(self):
        self.stacks = [
            mock_stack(name='vpc', fqn='namespace-vpc'),
            mock_stack(name='bucket', fqn='namespace-bucket'),
            mock_stack(name='role', fqn='separated-role'),
            mock_stack(name='dummy', fqn='namespace-dummy')
        ]
        self.context.get_stacks = Mock(return_value=self.stacks)
        self.outputs = {
            'vpc': {
                'VpcId': 'vpc-123456',
                'VpcName': 'dev'
            },
            'bucket': {
                'BucketName': 'my-bucket'
            },
            'role': {
                'RoleName': 'my-role',
                'RoleArn': 'arn:aws:iam::123456789012:role/my-role'
            },
            'dummy': {}
        }

    def _set_up_provider(self):
        self.provider = TestProvider()

        def provider_outputs():
            for stack in self.stacks:
                outputs = [{'OutputKey': key, 'OutputValue': value}
                           for key, value in self.outputs[stack.name].items()]
                yield stack.fqn, outputs

        self.provider.set_outputs(dict(provider_outputs()))

    def setUp(self):
        self.context = mock_context(namespace="namespace")
        self._set_up_stacks()
        self._set_up_provider()

    def run_action(self, output_format):
        provider_builder = MockProviderBuilder(self.provider)
        action = Action(self.context, provider_builder=provider_builder)
        action.execute(output_format=output_format)

    def test_output_json(self):
        with patch('sys.stdout', new=StringIO()) as fake_out:
            self.run_action(output_format='json')

        json_data = json.loads(fake_out.getvalue().strip())
        self.maxDiff = None
        self.assertEqual(
            json_data,
            {
                'stacks': {
                    'vpc': {
                        'fqn': 'namespace-vpc',
                        'outputs': self.outputs['vpc']
                    },
                    'bucket': {
                        'fqn': 'namespace-bucket',
                        'outputs': self.outputs['bucket']
                    },
                    'role': {
                        'fqn': 'separated-role',
                        'outputs': self.outputs['role']
                    },
                    'dummy': {
                        'fqn': 'namespace-dummy',
                        'outputs': self.outputs['dummy']
                    }
                }
            })

    def test_output_plain(self):
        with patch('sys.stdout', new=StringIO()) as fake_out:
            self.run_action(output_format='plain')

        lines = fake_out.getvalue().strip().splitlines()

        for stack_name, outputs in self.outputs.items():
            for key, value in outputs.items():
                line = '{}.{}={}'.format(stack_name, key, value)
                self.assertIn(line, lines)

    def test_output_log(self):
        log_name = 'stacker.actions.info'
        with LogCapture(log_name) as logs:
            self.run_action(output_format='log')

        def msg(s):
            return log_name, 'INFO', s

        def msgs():
            yield msg('Outputs for stacks: namespace')
            for stack in sorted(self.stacks, key=lambda s: s.fqn):
                yield msg(stack.fqn + ':')
                for key, value in sorted(self.outputs[stack.name].items()):
                    yield msg('\t{}: {}'.format(key, value))

        logs.check(*msgs())
