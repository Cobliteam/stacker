from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from builtins import object
import os
import sys
import logging
import threading

from ..dag import walk, ThreadedWalker, UnlimitedSemaphore
from ..plan import Graph, Plan, Step
from ..target import Target

import botocore.exceptions
from stacker.session_cache import get_session
from stacker.exceptions import HookExecutionFailed, PlanFailed
from stacker.status import COMPLETE, SKIPPED, FailedStatus
from stacker.util import ensure_s3_bucket, get_s3_endpoint

logger = logging.getLogger(__name__)

# After submitting a stack update/create, this controls how long we'll wait
# between calls to DescribeStacks to check on it's status. Most stack updates
# take at least a couple minutes, so 30 seconds is pretty reasonable and inline
# with the suggested value in
# https://github.com/boto/botocore/blob/1.6.1/botocore/data/cloudformation/2010-05-15/waiters-2.json#L22
#
# This can be controlled via an environment variable, mostly for testing.
STACK_POLL_TIME = int(os.environ.get("STACKER_STACK_POLL_TIME", 30))


def build_walker(concurrency):
    """This will return a function suitable for passing to
    :class:`stacker.plan.Plan` for walking the graph.

    If concurrency is 1 (no parallelism) this will return a simple topological
    walker that doesn't use any multithreading.

    If concurrency is 0, this will return a walker that will walk the graph as
    fast as the graph topology allows.

    If concurrency is greater than 1, it will return a walker that will only
    execute a maximum of concurrency steps at any given time.

    Returns:
        func: returns a function to walk a :class:`stacker.dag.DAG`.
    """
    if concurrency == 1:
        return walk

    semaphore = UnlimitedSemaphore()
    if concurrency > 1:
        semaphore = threading.Semaphore(concurrency)

    return ThreadedWalker(semaphore).walk


def stack_template_key_name(blueprint):
    """Given a blueprint, produce an appropriate key name.

    Args:
        blueprint (:class:`stacker.blueprints.base.Blueprint`): The blueprint
            object to create the key from.

    Returns:
        string: Key name resulting from blueprint.
    """
    name = blueprint.name
    return "stack_templates/%s/%s-%s.json" % (blueprint.context.get_fqn(name),
                                              name,
                                              blueprint.version)


def stack_template_url(bucket_name, blueprint, endpoint):
    """Produces an s3 url for a given blueprint.

    Args:
        bucket_name (string): The name of the S3 bucket where the resulting
            templates are stored.
        blueprint (:class:`stacker.blueprints.base.Blueprint`): The blueprint
            object to create the URL to.
        endpoint (string): The s3 endpoint used for the bucket.

    Returns:
        string: S3 URL.
    """
    key_name = stack_template_key_name(blueprint)
    return "%s/%s/%s" % (endpoint, bucket_name, key_name)


class BaseAction(object):

    """Actions perform the actual work of each Command.

    Each action is tied to a :class:`stacker.commands.base.BaseCommand`, and
    is responsible for building the :class:`stacker.plan.Plan` that will be
    executed to perform that command.

    Args:
        context (:class:`stacker.context.Context`): The stacker context for
            the current run.
        provider_builder (:class:`stacker.providers.base.BaseProviderBuilder`,
            optional): An object that will build a provider that will be
            interacted with in order to perform the necessary actions.
    """

    def __init__(self, context, provider_builder=None, cancel=None):
        self.context = context
        self.provider_builder = provider_builder
        self.bucket_name = context.bucket_name
        self.cancel = cancel or threading.Event()
        self.bucket_region = context.config.stacker_bucket_region
        if not self.bucket_region and provider_builder:
            self.bucket_region = provider_builder.region
        self.s3_conn = get_session(self.bucket_region).client('s3')

    def plan(self, description, action_name, action, context, tail=None,
             reverse=False, run_hooks=True):
        """A helper that builds a graph based plan from a set of stacks.

        Args:
            description (str): a description of the plan.
            action_name (str): name of the action being run. Used to generate
                target names and filter out which hooks to run.
            action (func): a function to call for each stack.
            context (stacker.context.Context): a context to build the plan
                from.
            tail (func): an optional function to call to tail the stack
                progress.
            reverse (bool): whether to flip the direction of dependencies.
                Use it when planning an action for destroying resources,
                which usually must happen in the reverse order of creation.
                Note: this does not change the order of execution of pre/post
                action hooks,  as the build and destroy hooks are currently
                configured in separate.
            run_hooks (bool): whether to run hooks configured for this action

        Returns: stacker.plan.Plan: the resulting plan for this action
        """

        def target_fn(*args, **kwargs):
            return COMPLETE

        def hook_fn(hook, *args, **kwargs):
            provider = self.provider_builder.build(profile=hook.profile,
                                                   region=hook.region)

            try:
                result = hook.run(provider, self.context)
            except HookExecutionFailed as e:
                return FailedStatus(reason=str(e))

            if result is None:
                return SKIPPED

            return COMPLETE

        pre_hooks_target = Target(
            name="pre_{}_hooks".format(action_name))
        pre_action_target = Target(
            name="pre_{}".format(action_name),
            requires=[pre_hooks_target.name])
        action_target = Target(
            name=action_name,
            requires=[pre_action_target.name])
        post_action_target = Target(
            name="post_{}".format(action_name),
            requires=[action_target.name])
        post_hooks_target = Target(
            name="post_{}_hooks".format(action_name),
            requires=[post_action_target.name])

        def steps():
            yield Step.from_target(pre_hooks_target, fn=target_fn)
            yield Step.from_target(pre_action_target, fn=target_fn)
            yield Step.from_target(action_target, fn=target_fn)
            yield Step.from_target(post_action_target, fn=target_fn)
            yield Step.from_target(post_hooks_target, fn=target_fn)

            if run_hooks:
                # Since we need to maintain compatibility with legacy hooks,
                # we separate them completely from the new hooks.
                # The legacy hooks will run in two separate phases, completely
                # isolated from regular stacks and targets, and any of the new
                # hooks.
                # Hence, all legacy pre-hooks will finish before any of the
                # new hooks, and all legacy post-hooks will only start after
                # the new hooks.

                hooks = self.context.get_hooks_for_action(action_name)
                logger.debug("Found hooks for action {}: {}".format(
                    action_name, hooks))

                for hook in hooks.pre:
                    yield Step.from_hook(
                        hook, fn=hook_fn,
                        required_by=[pre_hooks_target.name])

                for hook in hooks.custom:
                    step = Step.from_hook(
                        hook, fn=hook_fn)
                    if reverse:
                        step.reverse_requirements()

                    step.requires.add(pre_action_target.name)
                    step.required_by.add(post_action_target.name)
                    yield step

                for hook in hooks.post:
                    yield Step.from_hook(
                        hook, fn=hook_fn,
                        requires=[post_hooks_target.name])

            for target in context.get_targets():
                step = Step.from_target(target, fn=target_fn)
                if reverse:
                    step.reverse_requirements()

                yield step

            for stack in context.get_stacks():
                step = Step.from_stack(stack, fn=action, watch_func=tail)
                if reverse:
                    step.reverse_requirements()

                # Contain stack execution in the boundaries of the pre_action
                # and post_action targets.
                step.requires.add(pre_action_target.name)
                step.required_by.add(action_target.name)

                yield step

        graph = Graph.from_steps(list(steps()))

        return Plan.from_graph(
            description=description,
            graph=graph,
            targets=context.stack_names)

    def ensure_cfn_bucket(self):
        """The CloudFormation bucket where templates will be stored."""
        if self.bucket_name:
            ensure_s3_bucket(self.s3_conn,
                             self.bucket_name,
                             self.bucket_region)

    def stack_template_url(self, blueprint):
        return stack_template_url(
            self.bucket_name, blueprint, get_s3_endpoint(self.s3_conn)
        )

    def s3_stack_push(self, blueprint, force=False):
        """Pushes the rendered blueprint's template to S3.

        Verifies that the template doesn't already exist in S3 before
        pushing.

        Returns the URL to the template in S3.
        """
        key_name = stack_template_key_name(blueprint)
        template_url = self.stack_template_url(blueprint)
        try:
            template_exists = self.s3_conn.head_object(
                Bucket=self.bucket_name, Key=key_name) is not None
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == '404':
                template_exists = False
            else:
                raise

        if template_exists and not force:
            logger.debug("Cloudformation template %s already exists.",
                         template_url)
            return template_url
        self.s3_conn.put_object(Bucket=self.bucket_name,
                                Key=key_name,
                                Body=blueprint.rendered,
                                ServerSideEncryption='AES256',
                                ACL='bucket-owner-full-control')
        logger.debug("Blueprint %s pushed to %s.", blueprint.name,
                     template_url)
        return template_url

    def execute(self, *args, **kwargs):
        try:
            self.pre_run(*args, **kwargs)
            self.run(*args, **kwargs)
            self.post_run(*args, **kwargs)
        except PlanFailed as e:
            logger.error(str(e))
            sys.exit(1)

    def pre_run(self, *args, **kwargs):
        pass

    def run(self, *args, **kwargs):
        raise NotImplementedError("Subclass must implement \"run\" method")

    def post_run(self, *args, **kwargs):
        pass

    def build_provider(self, stack):
        """Builds a :class:`stacker.providers.base.Provider` suitable for
        operating on the given :class:`stacker.Stack`."""
        return self.provider_builder.build(region=stack.region,
                                           profile=stack.profile)

    @property
    def provider(self):
        """Some actions need a generic provider using the default region (e.g.
        hooks)."""
        return self.provider_builder.build()

    def _tail_stack(self, stack, cancel, retries=0, **kwargs):
        provider = self.build_provider(stack)
        return provider.tail_stack(stack, cancel, retries, **kwargs)
