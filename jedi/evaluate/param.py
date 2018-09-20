from collections import defaultdict

from jedi.evaluate.utils import PushBackIterator
from jedi.evaluate import analysis
from jedi.evaluate.lazy_context import LazyKnownContext, \
    LazyTreeContext, LazyUnknownContext
from jedi.evaluate import docstrings
from jedi.evaluate.context import iterable


def _add_argument_issue(parent_context, error_name, lazy_context, message):
    if isinstance(lazy_context, LazyTreeContext):
        node = lazy_context.data
        if node.parent.type == 'argument':
            node = node.parent
        analysis.add(parent_context, error_name, node, message)


class ExecutedParam(object):
    """Fake a param and give it values."""
    def __init__(self, execution_context, param_node, lazy_context):
        self._execution_context = execution_context
        self._param_node = param_node
        self._lazy_context = lazy_context
        self.string_name = param_node.name.value

    def infer_annotations(self):
        from jedi.evaluate import pep0484
        return pep0484.infer_param(self._execution_context, self._param_node)

    def infer(self, use_hints=True):
        if use_hints:
            doc_params = docstrings.infer_param(self._execution_context, self._param_node)
            ann = self.infer_annotations().execute_annotation()
            if ann or doc_params:
                return ann | doc_params

        return self._lazy_context.infer()

    def matches_signature(self):
        argument_contexts = self.infer(use_hints=False).py__class__()
        if self._param_node.star_count:
            return True
        annotations = self.infer_annotations()
        if not annotations:
            # If we cannot infer annotations - or there aren't any - pretend
            # that the signature matches.
            return True
        return any(c1.is_sub_class_of(c2)
                   for c1 in argument_contexts
                   for c2 in annotations)

    @property
    def var_args(self):
        return self._execution_context.var_args

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self.string_name)


def get_executed_params(execution_context, var_args):
    result_params = []
    param_dict = {}
    funcdef = execution_context.tree_node
    # Default params are part of the context where the function was defined.
    # This means that they might have access on class variables that the
    # function itself doesn't have.
    default_param_context = execution_context.function_context.get_default_param_context()

    for param in funcdef.get_params():
        param_dict[param.name.value] = param
    unpacked_va = list(var_args.unpack(funcdef))
    var_arg_iterator = PushBackIterator(iter(unpacked_va))

    non_matching_keys = defaultdict(lambda: [])
    keys_used = {}
    keys_only = False
    had_multiple_value_error = False
    for param in funcdef.get_params():
        # The value and key can both be null. There, the defaults apply.
        # args / kwargs will just be empty arrays / dicts, respectively.
        # Wrong value count is just ignored. If you try to test cases that are
        # not allowed in Python, Jedi will maybe not show any completions.
        key, argument = next(var_arg_iterator, (None, None))
        while key is not None:
            keys_only = True
            try:
                key_param = param_dict[key]
            except KeyError:
                non_matching_keys[key] = argument
            else:
                if key in keys_used:
                    had_multiple_value_error = True
                    m = ("TypeError: %s() got multiple values for keyword argument '%s'."
                         % (funcdef.name, key))
                    for node in var_args.get_calling_nodes():
                        analysis.add(default_param_context, 'type-error-multiple-values',
                                     node, message=m)
                else:
                    keys_used[key] = ExecutedParam(execution_context, key_param, argument)
            key, argument = next(var_arg_iterator, (None, None))

        try:
            result_params.append(keys_used[param.name.value])
            continue
        except KeyError:
            pass

        if param.star_count == 1:
            # *args param
            lazy_context_list = []
            if argument is not None:
                lazy_context_list.append(argument)
                for key, argument in var_arg_iterator:
                    # Iterate until a key argument is found.
                    if key:
                        var_arg_iterator.push_back((key, argument))
                        break
                    lazy_context_list.append(argument)
            seq = iterable.FakeSequence(execution_context.evaluator, u'tuple', lazy_context_list)
            result_arg = LazyKnownContext(seq)
        elif param.star_count == 2:
            # **kwargs param
            dct = iterable.FakeDict(execution_context.evaluator, dict(non_matching_keys))
            result_arg = LazyKnownContext(dct)
            non_matching_keys = {}
        else:
            # normal param
            if argument is None:
                # No value: Return an empty container
                if param.default is None:
                    result_arg = LazyUnknownContext()
                    if not keys_only:
                        for node in var_args.get_calling_nodes():
                            m = _error_argument_count(funcdef, len(unpacked_va))
                            analysis.add(default_param_context, 'type-error-too-few-arguments',
                                         node, message=m)
                else:
                    result_arg = LazyTreeContext(default_param_context, param.default)
            else:
                result_arg = argument

        result_params.append(ExecutedParam(execution_context, param, result_arg))
        if not isinstance(result_arg, LazyUnknownContext):
            keys_used[param.name.value] = result_params[-1]

    if keys_only:
        # All arguments should be handed over to the next function. It's not
        # about the values inside, it's about the names. Jedi needs to now that
        # there's nothing to find for certain names.
        for k in set(param_dict) - set(keys_used):
            param = param_dict[k]

            if not (non_matching_keys or had_multiple_value_error or
                    param.star_count or param.default):
                # add a warning only if there's not another one.
                for node in var_args.get_calling_nodes():
                    m = _error_argument_count(funcdef, len(unpacked_va))
                    analysis.add(default_param_context, 'type-error-too-few-arguments',
                                 node, message=m)

    for key, lazy_context in non_matching_keys.items():
        m = "TypeError: %s() got an unexpected keyword argument '%s'." \
            % (funcdef.name, key)
        _add_argument_issue(
            default_param_context,
            'type-error-keyword-argument',
            lazy_context,
            message=m
        )

    remaining_arguments = list(var_arg_iterator)
    if remaining_arguments:
        m = _error_argument_count(funcdef, len(unpacked_va))
        # Just report an error for the first param that is not needed (like
        # cPython).
        first_key, lazy_context = remaining_arguments[0]
        if var_args.get_calling_nodes():
            # There might not be a valid calling node so check for that first.
            _add_argument_issue(
                default_param_context,
                'type-error-too-many-arguments',
                lazy_context,
                message=m
            )
    return result_params


def _error_argument_count(funcdef, actual_count):
    params = funcdef.get_params()
    default_arguments = sum(1 for p in params if p.default or p.star_count)

    if default_arguments == 0:
        before = 'exactly '
    else:
        before = 'from %s to ' % (len(params) - default_arguments)
    return ('TypeError: %s() takes %s%s arguments (%s given).'
            % (funcdef.name, before, len(params), actual_count))


def _create_default_param(execution_context, param):
    if param.star_count == 1:
        result_arg = LazyKnownContext(
            iterable.FakeSequence(execution_context.evaluator, u'tuple', [])
        )
    elif param.star_count == 2:
        result_arg = LazyKnownContext(
            iterable.FakeDict(execution_context.evaluator, {})
        )
    elif param.default is None:
        result_arg = LazyUnknownContext()
    else:
        result_arg = LazyTreeContext(execution_context.parent_context, param.default)
    return ExecutedParam(execution_context, param, result_arg)


def create_default_params(execution_context, funcdef):
    return [_create_default_param(execution_context, p)
            for p in funcdef.get_params()]
