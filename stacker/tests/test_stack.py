from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from mock import MagicMock
import unittest

from stacker.context import Context
from stacker.config import Config
from stacker.stack import Stack, ExternalStack
from .factories import generate_definition, generate_external_definition


class TestStack(unittest.TestCase):

    def setUp(self):
        self.sd = {"name": "test"}
        self.config = Config({"namespace": "namespace"})
        self.context = Context(config=self.config)
        self.stack = Stack(
            definition=generate_definition("vpc", 1),
            context=self.context,
        )

    def test_stack_requires(self):
        definition = generate_definition(
            base_name="vpc",
            stack_id=1,
            variables={
                "Var1": "${noop fakeStack3::FakeOutput}",
                "Var2": (
                    "some.template.value:${output fakeStack2::FakeOutput}:"
                    "${output fakeStack::FakeOutput}"
                ),
                "Var3": "${output fakeStack::FakeOutput},"
                        "${output fakeStack2::FakeOutput}",
            },
            requires=["fakeStack"],
        )
        stack = Stack(definition=definition, context=self.context)
        self.assertEqual(len(stack.requires), 2)
        self.assertIn(
            "fakeStack",
            stack.requires,
        )
        self.assertIn(
            "fakeStack2",
            stack.requires,
        )

    def test_stack_requires_circular_ref(self):
        definition = generate_definition(
            base_name="vpc",
            stack_id=1,
            variables={
                "Var1": "${output vpc.1::FakeOutput}",
            },
        )
        stack = Stack(definition=definition, context=self.context)
        with self.assertRaises(ValueError):
            stack.requires

    def test_stack_cfn_parameters(self):
        definition = generate_definition(
            base_name="vpc",
            stack_id=1,
            variables={
                "Param1": "${output fakeStack::FakeOutput}",
            },
        )
        stack = Stack(definition=definition, context=self.context)
        stack._blueprint = MagicMock()
        stack._blueprint.get_parameter_values.return_value = {
            "Param2": "Some Resolved Value",
        }
        self.assertEqual(len(stack.parameter_values), 1)
        param = stack.parameter_values["Param2"]
        self.assertEqual(param, "Some Resolved Value")

    def test_stack_tags_default(self):
        self.config.tags = {"environment": "prod"}
        definition = generate_definition(
            base_name="vpc",
            stack_id=1
        )
        stack = Stack(definition=definition, context=self.context)
        self.assertEquals(stack.tags, {"environment": "prod"})

    def test_stack_tags_override(self):
        self.config.tags = {"environment": "prod"}
        definition = generate_definition(
            base_name="vpc",
            stack_id=1,
            tags={"environment": "stage"}
        )
        stack = Stack(definition=definition, context=self.context)
        self.assertEquals(stack.tags, {"environment": "stage"})

    def test_stack_tags_extra(self):
        self.config.tags = {"environment": "prod"}
        definition = generate_definition(
            base_name="vpc",
            stack_id=1,
            tags={"app": "graph"}
        )
        stack = Stack(definition=definition, context=self.context)
        self.assertEquals(stack.tags, {"environment": "prod", "app": "graph"})

    def test_stack_should_update(self):
        test_scenarios = [
            dict(locked=False, force=False, result=True),
            dict(locked=False, force=True, result=True),
            dict(locked=True, force=False, result=False),
            dict(locked=True, force=True, result=True)
        ]

        for t in test_scenarios:
            definition = generate_definition(
                base_name="vpc",
                stack_id=1,
                locked=t['locked'])
            stack = Stack(definition=definition, context=self.context,
                          force=t['force'])
            self.assertEqual(stack.should_update(), t['result'])

    def test_stack_should_submit(self):
        for enabled in (True, False):
            definition = generate_definition(
                base_name="vpc",
                stack_id=1,
                enabled=enabled)
            stack = Stack(definition=definition, context=self.context)
            self.assertEqual(stack.should_submit(), enabled)

    def test_external_stack_should_update(self):
        definition = generate_external_definition(
            base_name="vpc",
            stack_id=1)
        stack = ExternalStack(definition=definition, context=self.context)
        self.assertEqual(stack.should_update(), False)

    def test_external_stack_should_submit(self):
        definition = generate_external_definition(
            base_name="vpc",
            stack_id=1)
        stack = ExternalStack(definition=definition, context=self.context)
        self.assertEqual(stack.should_submit(), True)


if __name__ == '__main__':
    unittest.main()
