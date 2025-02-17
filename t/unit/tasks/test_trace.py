from unittest.mock import ANY, Mock, PropertyMock, patch
from uuid import uuid4

import pytest
from billiard.einfo import ExceptionInfo
from kombu.exceptions import EncodeError

from celery import group, signals, states, uuid
from celery.app.task import Context
from celery.app.trace import (TraceInfo, build_tracer, fast_trace_task, get_log_policy, get_task_name,
                              log_policy_expected, log_policy_ignore, log_policy_internal, log_policy_reject,
                              log_policy_unexpected, reset_worker_optimizations, setup_worker_optimizations,
                              trace_task, trace_task_ret, traceback_clear)
from celery.backends.base import BaseDictBackend
from celery.backends.cache import CacheBackend
from celery.exceptions import BackendGetMetaError, Ignore, Reject, Retry
from celery.states import PENDING
from celery.worker.state import successful_requests


def trace(
    app, task, args=(), kwargs={}, propagate=False,
    eager=True, request=None, task_id='id-1', **opts
):
    t = build_tracer(task.name, task, eager=eager, propagate=propagate, app=app, **opts)
    ret = t(task_id, args, kwargs, request)
    return ret.retval, ret.info, ret.runtime


class TraceCase:
    def setup_method(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        self.add = add

        @self.app.task(shared=False, ignore_result=True)
        def add_cast(x, y):
            return x + y

        self.add_cast = add_cast

        @self.app.task(shared=False)
        def raises(exc):
            raise exc

        self.raises = raises

    def trace(self, *args, **kwargs):
        return trace(self.app, *args, **kwargs)


class test_trace(TraceCase):
    def test_trace_successful(self):
        retval, info, _ = self.trace(self.add, (2, 2), {})
        assert info is None
        assert retval == 4

    def test_trace_before_start(self):
        @self.app.task(shared=False, before_start=Mock())
        def add_with_before_start(x, y):
            return x + y

        self.trace(add_with_before_start, (2, 2), {})
        add_with_before_start.before_start.assert_called()

    def test_trace_on_success(self):
        @self.app.task(shared=False, on_success=Mock())
        def add_with_success(x, y):
            return x + y

        self.trace(add_with_success, (2, 2), {})
        add_with_success.on_success.assert_called()

    def test_get_log_policy(self):
        einfo = Mock(name='einfo')
        einfo.internal = False
        assert get_log_policy(self.add, einfo, Reject()) is log_policy_reject
        assert get_log_policy(self.add, einfo, Ignore()) is log_policy_ignore

        self.add.throws = (TypeError,)
        assert get_log_policy(self.add, einfo, KeyError()) is log_policy_unexpected
        assert get_log_policy(self.add, einfo, TypeError()) is log_policy_expected

        einfo2 = Mock(name='einfo2')
        einfo2.internal = True
        assert get_log_policy(self.add, einfo2, KeyError()) is log_policy_internal

    def test_get_task_name(self):
        assert get_task_name(Context({}), 'default') == 'default'
        assert get_task_name(Context({'shadow': None}), 'default') == 'default'
        assert get_task_name(Context({'shadow': ''}), 'default') == 'default'
        assert get_task_name(Context({'shadow': 'test'}), 'default') == 'test'

    def test_trace_after_return(self):
        @self.app.task(shared=False, after_return=Mock())
        def add_with_after_return(x, y):
            return x + y

        self.trace(add_with_after_return, (2, 2), {})
        add_with_after_return.after_return.assert_called()

    def test_with_prerun_receivers(self):
        on_prerun = Mock()
        signals.task_prerun.connect(on_prerun)
        try:
            self.trace(self.add, (2, 2), {})
            on_prerun.assert_called()
        finally:
            signals.task_prerun.receivers[:] = []

    def test_with_postrun_receivers(self):
        on_postrun = Mock()
        signals.task_postrun.connect(on_postrun)
        try:
            self.trace(self.add, (2, 2), {})
            on_postrun.assert_called()
        finally:
            signals.task_postrun.receivers[:] = []

    def test_with_success_receivers(self):
        on_success = Mock()
        signals.task_success.connect(on_success)
        try:
            _, _, expected_runtime = self.trace(self.add, (2, 2), {})
            on_success.assert_called()
            runtime = on_success.call_args[1]['runtime']
            assert expected_runtime == runtime
        finally:
            signals.task_success.receivers[:] = []

    def test_when_chord_part(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        add.backend = Mock()

        request = {'chord': uuid()}
        self.trace(add, (2, 2), {}, request=request)
        add.backend.mark_as_done.assert_called()
        args, kwargs = add.backend.mark_as_done.call_args
        assert args[0] == 'id-1'
        assert args[1] == 4
        assert args[2].chord == request['chord']
        assert not args[3]

    def test_when_backend_cleanup_raises(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        add.backend = Mock(name='backend')
        add.backend.process_cleanup.side_effect = KeyError()
        self.trace(add, (2, 2), {}, eager=False)
        add.backend.process_cleanup.assert_called_with()
        add.backend.process_cleanup.side_effect = MemoryError()
        with pytest.raises(MemoryError):
            self.trace(add, (2, 2), {}, eager=False)

    def test_eager_task_does_not_store_result_even_if_not_ignore_result(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        add.backend = Mock(name='backend')
        add.ignore_result = False

        self.trace(add, (2, 2), {}, eager=True)

        add.backend.mark_as_done.assert_called_once_with(
            'id-1',     # task_id
            4,          # result
            ANY,        # request
            False       # store_result
        )

    def test_eager_task_does_not_call_store_result(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        backend = BaseDictBackend(app=self.app)
        backend.store_result = Mock()
        add.backend = backend
        add.ignore_result = False

        self.trace(add, (2, 2), {}, eager=True)

        add.backend.store_result.assert_not_called()

    def test_eager_task_will_store_result_if_proper_setting_is_set(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        add.backend = Mock(name='backend')
        add.store_eager_result = True
        add.ignore_result = False

        self.trace(add, (2, 2), {}, eager=True)

        add.backend.mark_as_done.assert_called_once_with(
            'id-1',     # task_id
            4,          # result
            ANY,        # request
            True        # store_result
        )

    def test_eager_task_with_setting_will_call_store_result(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        backend = BaseDictBackend(app=self.app)
        backend.store_result = Mock()
        add.backend = backend
        add.store_eager_result = True
        add.ignore_result = False

        self.trace(add, (2, 2), {}, eager=True)

        add.backend.store_result.assert_called_once_with(
            'id-1',
            4,
            states.SUCCESS,
            request=ANY
        )

    def test_when_backend_raises_exception(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        add.backend = Mock(name='backend')
        add.backend.mark_as_done.side_effect = Exception()
        add.backend.mark_as_failure.side_effect = Exception("failed mark_as_failure")

        with pytest.raises(Exception):
            self.trace(add, (2, 2), {}, eager=False)

    def test_traceback_clear(self):
        import inspect
        import sys
        sys.exc_clear = Mock()
        frame_list = []

        def raise_dummy():
            frame_str_temp = str(inspect.currentframe().__repr__)
            frame_list.append(frame_str_temp)
            raise KeyError('foo')

        try:
            raise_dummy()
        except KeyError as exc:
            traceback_clear(exc)

            tb_ = exc.__traceback__
            while tb_ is not None:
                if str(tb_.tb_frame.__repr__) == frame_list[0]:
                    assert len(tb_.tb_frame.f_locals) == 0
                tb_ = tb_.tb_next

        try:
            raise_dummy()
        except KeyError as exc:
            traceback_clear()

            tb_ = exc.__traceback__
            while tb_ is not None:
                if str(tb_.tb_frame.__repr__) == frame_list[0]:
                    assert len(tb_.tb_frame.f_locals) == 0
                tb_ = tb_.tb_next

        try:
            raise_dummy()
        except KeyError as exc:
            traceback_clear(str(exc))

            tb_ = exc.__traceback__
            while tb_ is not None:
                if str(tb_.tb_frame.__repr__) == frame_list[0]:
                    assert len(tb_.tb_frame.f_locals) == 0
                tb_ = tb_.tb_next

    @patch('celery.app.trace.traceback_clear')
    def test_when_Ignore(self, mock_traceback_clear):
        @self.app.task(shared=False)
        def ignored():
            raise Ignore()

        retval, info, _ = self.trace(ignored, (), {})
        assert info.state == states.IGNORED
        mock_traceback_clear.assert_called()

    @patch('celery.app.trace.traceback_clear')
    def test_when_Reject(self, mock_traceback_clear):
        @self.app.task(shared=False)
        def rejecting():
            raise Reject()

        retval, info, _ = self.trace(rejecting, (), {})
        assert info.state == states.REJECTED
        mock_traceback_clear.assert_called()

    def test_backend_cleanup_raises(self):
        self.add.backend.process_cleanup = Mock()
        self.add.backend.process_cleanup.side_effect = RuntimeError()
        self.trace(self.add, (2, 2), {})

    @patch('celery.canvas.maybe_signature')
    def test_callbacks__scalar(self, maybe_signature):
        sig = Mock(name='sig')
        request = {'callbacks': [sig], 'root_id': 'root'}
        maybe_signature.return_value = sig
        retval, _, _ = self.trace(self.add, (2, 2), {}, request=request)
        sig.apply_async.assert_called_with(
            (4,), parent_id='id-1', root_id='root', priority=None
        )

    @patch('celery.canvas.maybe_signature')
    def test_chain_proto2(self, maybe_signature):
        sig = Mock(name='sig')
        sig2 = Mock(name='sig2')
        request = {'chain': [sig2, sig], 'root_id': 'root'}
        maybe_signature.return_value = sig
        retval, _, _ = self.trace(self.add, (2, 2), {}, request=request)
        sig.apply_async.assert_called_with(
            (4,), parent_id='id-1', root_id='root', chain=[sig2], priority=None
        )

    @patch('celery.canvas.maybe_signature')
    def test_chain_inherit_parent_priority(self, maybe_signature):
        self.app.conf.task_inherit_parent_priority = True
        sig = Mock(name='sig')
        sig2 = Mock(name='sig2')
        request = {
            'chain': [sig2, sig],
            'root_id': 'root',
            'delivery_info': {'priority': 42},
        }
        maybe_signature.return_value = sig
        retval, _, _ = self.trace(self.add, (2, 2), {}, request=request)
        sig.apply_async.assert_called_with(
            (4,), parent_id='id-1', root_id='root', chain=[sig2], priority=42
        )

    @patch('celery.canvas.maybe_signature')
    def test_callbacks__EncodeError(self, maybe_signature):
        sig = Mock(name='sig')
        request = {'callbacks': [sig], 'root_id': 'root'}
        maybe_signature.return_value = sig
        sig.apply_async.side_effect = EncodeError()
        retval, einfo, _ = self.trace(self.add, (2, 2), {}, request=request)
        assert einfo.state == states.FAILURE

    @patch('celery.canvas.maybe_signature')
    @patch('celery.app.trace.group.apply_async')
    def test_callbacks__sigs(self, group_, maybe_signature):
        sig1 = Mock(name='sig')
        sig2 = Mock(name='sig2')
        sig3 = group([Mock(name='g1'), Mock(name='g2')], app=self.app)
        sig3.apply_async = Mock(name='gapply')
        request = {'callbacks': [sig1, sig3, sig2], 'root_id': 'root'}

        def pass_value(s, *args, **kwargs):
            return s

        maybe_signature.side_effect = pass_value
        retval, _, _ = self.trace(self.add, (2, 2), {}, request=request)
        group_.assert_called_with((4,), parent_id='id-1', root_id='root', priority=None)
        sig3.apply_async.assert_called_with(
            (4,), parent_id='id-1', root_id='root', priority=None
        )

    @patch('celery.canvas.maybe_signature')
    @patch('celery.app.trace.group.apply_async')
    def test_callbacks__only_groups(self, group_, maybe_signature):
        sig1 = group([Mock(name='g1'), Mock(name='g2')], app=self.app)
        sig2 = group([Mock(name='g3'), Mock(name='g4')], app=self.app)
        sig1.apply_async = Mock(name='gapply')
        sig2.apply_async = Mock(name='gapply')
        request = {'callbacks': [sig1, sig2], 'root_id': 'root'}

        def pass_value(s, *args, **kwargs):
            return s

        maybe_signature.side_effect = pass_value
        retval, _, _ = self.trace(self.add, (2, 2), {}, request=request)
        sig1.apply_async.assert_called_with(
            (4,), parent_id='id-1', root_id='root', priority=None
        )
        sig2.apply_async.assert_called_with(
            (4,), parent_id='id-1', root_id='root', priority=None
        )

    def test_trace_SystemExit(self):
        with pytest.raises(SystemExit):
            self.trace(self.raises, (SystemExit(),), {})

    @patch('celery.app.trace.traceback_clear')
    def test_trace_Retry(self, mock_traceback_clear):
        exc = Retry('foo', 'bar')
        _, info, _ = self.trace(self.raises, (exc,), {})
        assert info.state == states.RETRY
        assert info.retval is exc
        mock_traceback_clear.assert_called()

    @patch('celery.app.trace.traceback_clear')
    def test_trace_exception(self, mock_traceback_clear):
        exc = KeyError('foo')
        _, info, _ = self.trace(self.raises, (exc,), {})
        assert info.state == states.FAILURE
        assert info.retval is exc
        mock_traceback_clear.assert_called()

    def test_trace_task_ret__no_content_type(self):
        trace_task_ret(
            self.add.name, 'id1', {}, ((2, 2), {}, {}), None, None, app=self.app,
        )

    def test_fast_trace_task__no_content_type(self):
        self.app.tasks[self.add.name].__trace__ = build_tracer(
            self.add.name, self.add, app=self.app,
        )
        fast_trace_task(
            self.add.name,
            'id1',
            {},
            ((2, 2), {}, {}),
            None,
            None,
            app=self.app,
            _loc=[self.app.tasks, {}, 'hostname'],
        )

    def test_trace_exception_propagate(self):
        with pytest.raises(KeyError):
            self.trace(self.raises, (KeyError('foo'),), {}, propagate=True)

    @patch('celery.app.trace.signals.task_internal_error.send')
    @patch('celery.app.trace.build_tracer')
    @patch('celery.app.trace.report_internal_error')
    def test_outside_body_error(self, report_internal_error, build_tracer, send):
        tracer = Mock()
        tracer.side_effect = KeyError('foo')
        build_tracer.return_value = tracer

        @self.app.task(shared=False)
        def xtask():
            pass

        trace_task(xtask, 'uuid', (), {})
        assert report_internal_error.call_count
        assert send.call_count
        assert xtask.__trace__ is tracer

    def test_backend_error_should_report_failure(self):
        """check internal error is reported as failure.

        In case of backend error, an exception may bubble up from trace and be
        caught by trace_task.
        """

        @self.app.task(shared=False)
        def xtask():
            pass

        xtask.backend = BaseDictBackend(app=self.app)
        xtask.backend.mark_as_done = Mock()
        xtask.backend.mark_as_done.side_effect = Exception()
        xtask.backend.mark_as_failure = Mock()
        xtask.backend.mark_as_failure.side_effect = Exception()

        ret, info, _, _ = trace_task(xtask, 'uuid', (), {}, app=self.app)
        assert info is not None
        assert isinstance(ret, ExceptionInfo)

    def test_deduplicate_successful_tasks__deduplication(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        backend = CacheBackend(app=self.app, backend='memory')
        add.backend = backend
        add.store_eager_result = True
        add.ignore_result = False
        add.acks_late = True

        self.app.conf.worker_deduplicate_successful_tasks = True
        task_id = str(uuid4())
        request = {'id': task_id, 'delivery_info': {'redelivered': True}}

        assert trace(self.app, add, (1, 1), task_id=task_id, request=request) == (2, None, ANY)
        assert trace(self.app, add, (1, 1), task_id=task_id, request=request) == (None, None, ANY)

        self.app.conf.worker_deduplicate_successful_tasks = False

    def test_deduplicate_successful_tasks__no_deduplication(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        backend = CacheBackend(app=self.app, backend='memory')
        add.backend = backend
        add.store_eager_result = True
        add.ignore_result = False
        add.acks_late = True

        self.app.conf.worker_deduplicate_successful_tasks = True
        task_id = str(uuid4())
        request = {'id': task_id, 'delivery_info': {'redelivered': True}}

        with patch('celery.app.trace.AsyncResult') as async_result_mock:
            async_result_mock().state.return_value = PENDING
            assert trace(self.app, add, (1, 1), task_id=task_id, request=request) == (2, None, ANY)
            assert trace(self.app, add, (1, 1), task_id=task_id, request=request) == (2, None, ANY)

        self.app.conf.worker_deduplicate_successful_tasks = False

    def test_deduplicate_successful_tasks__result_not_found(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        backend = CacheBackend(app=self.app, backend='memory')
        add.backend = backend
        add.store_eager_result = True
        add.ignore_result = False
        add.acks_late = True

        self.app.conf.worker_deduplicate_successful_tasks = True
        task_id = str(uuid4())
        request = {'id': task_id, 'delivery_info': {'redelivered': True}}

        with patch('celery.app.trace.AsyncResult') as async_result_mock:
            assert trace(self.app, add, (1, 1), task_id=task_id, request=request) == (2, None, ANY)
            state_property = PropertyMock(side_effect=BackendGetMetaError)
            type(async_result_mock()).state = state_property
            assert trace(self.app, add, (1, 1), task_id=task_id, request=request) == (2, None, ANY)

        self.app.conf.worker_deduplicate_successful_tasks = False

    def test_deduplicate_successful_tasks__cached_request(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        backend = CacheBackend(app=self.app, backend='memory')
        add.backend = backend
        add.store_eager_result = True
        add.ignore_result = False
        add.acks_late = True

        self.app.conf.worker_deduplicate_successful_tasks = True

        task_id = str(uuid4())
        request = {'id': task_id, 'delivery_info': {'redelivered': True}}

        successful_requests.add(task_id)

        assert trace(self.app, add, (1, 1), task_id=task_id,
                     request=request) == (None, None, ANY)

        successful_requests.clear()
        self.app.conf.worker_deduplicate_successful_tasks = False


class test_TraceInfo(TraceCase):
    class TI(TraceInfo):
        __slots__ = TraceInfo.__slots__ + ('__dict__',)

    def test_handle_error_state(self):
        x = self.TI(states.FAILURE)
        x.handle_failure = Mock()
        x.handle_error_state(self.add_cast, self.add_cast.request)
        x.handle_failure.assert_called_with(
            self.add_cast,
            self.add_cast.request,
            store_errors=self.add_cast.store_errors_even_if_ignored,
            call_errbacks=True,
        )

    def test_handle_error_state_for_eager_task(self):
        x = self.TI(states.FAILURE)
        x.handle_failure = Mock()

        x.handle_error_state(self.add, self.add.request, eager=True)
        x.handle_failure.assert_called_once_with(
            self.add,
            self.add.request,
            store_errors=False,
            call_errbacks=True,
        )

    def test_handle_error_for_eager_saved_to_backend(self):
        x = self.TI(states.FAILURE)
        x.handle_failure = Mock()

        self.add.store_eager_result = True

        x.handle_error_state(self.add, self.add.request, eager=True)
        x.handle_failure.assert_called_with(
            self.add,
            self.add.request,
            store_errors=True,
            call_errbacks=True,
        )

    @patch('celery.app.trace.ExceptionInfo')
    def test_handle_reject(self, ExceptionInfo):
        x = self.TI(states.FAILURE)
        x._log_error = Mock(name='log_error')
        req = Mock(name='req')
        x.handle_reject(self.add, req)
        x._log_error.assert_called_with(self.add, req, ExceptionInfo())


class test_stackprotection:
    def test_stackprotection(self):
        setup_worker_optimizations(self.app)
        try:

            @self.app.task(shared=False, bind=True)
            def foo(self, i):
                if i:
                    return foo(0)
                return self.request

            assert foo(1).called_directly
        finally:
            reset_worker_optimizations(self.app)

    def test_stackprotection_headers_passed_on_new_request_stack(self):
        setup_worker_optimizations(self.app)
        try:

            @self.app.task(shared=False, bind=True)
            def foo(self, i):
                if i:
                    return foo.apply(args=(i-1,), headers=456)
                return self.request

            task = foo.apply(args=(2,), headers=123, loglevel=5)
            assert task.result.result.result.args == (0,)
            assert task.result.result.result.headers == 456
            assert task.result.result.result.loglevel == 0
        finally:
            reset_worker_optimizations(self.app)

    def test_stackprotection_headers_persisted_calling_task_directly(self):
        setup_worker_optimizations(self.app)
        try:

            @self.app.task(shared=False, bind=True)
            def foo(self, i):
                if i:
                    return foo(i-1)
                return self.request

            task = foo.apply(args=(2,), headers=123, loglevel=5)
            assert task.result.args == (0,)
            assert task.result.headers == 123
            assert task.result.loglevel == 5
        finally:
            reset_worker_optimizations(self.app)
