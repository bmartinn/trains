import os
import psutil
import sys
from multiprocessing import Process, Lock, Event as PrEvent
from multiprocessing.pool import ThreadPool
from threading import Thread, Event as TrEvent
from time import sleep
from typing import List

from ..py3_interop import AbstractContextManager
from ...debugging.trace import stdout_print

try:
    from multiprocessing import SimpleQueue
except ImportError:  # noqa
    from multiprocessing.queues import SimpleQueue


class SafeQueue(object):
    __lock = None
    __thread_pool = None
    __thread_pool_pid = None

    def __init__(self, *args, **kwargs):
        self._q = SimpleQueue(*args, **kwargs)
        if not SafeQueue.__lock:
            SafeQueue.__lock = Lock()
        if not SafeQueue.__thread_pool:
            with SafeQueue.__lock:
                if not SafeQueue.__thread_pool:
                    SafeQueue.__thread_pool_pid = os.getpid()
                    SafeQueue.__thread_pool = ThreadPool(processes=1)

    def empty(self):
        return self._q.empty()

    def get(self):
        return self._q.get()

    def put(self, obj):
        # if this is a new sub_process
        if os.getpid() != SafeQueue.__thread_pool_pid:
            # get the lock
            with SafeQueue.__lock:
                # check if we need to create a new thread pool (now atomic)
                if os.getpid() != SafeQueue.__thread_pool_pid:
                    SafeQueue.__thread_pool_pid = os.getpid()
                    SafeQueue.__thread_pool = ThreadPool(processes=1)
        # make sure the block put is done in the thread pool i.e. in the background
        SafeQueue.__thread_pool.apply_async(self._q.put, args=(obj, ))


class SingletonLock(AbstractContextManager):
    _instances = []

    def __init__(self):
        self._lock = None
        SingletonLock._instances.append(self)

    def acquire(self, *args, **kwargs):
        self.create()
        return self._lock.acquire(*args, **kwargs)

    def release(self, *args, **kwargs):
        if self._lock is None:
            return None
        return self._lock.release(*args, **kwargs)

    def create(self):
        if self._lock is None:
            self._lock = Lock()

    @classmethod
    def instantiate(cls):
        for i in cls._instances:
            i.create()

    def __enter__(self):
        """Return `self` upon entering the runtime context."""
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Raise any exception triggered within the runtime context."""
        # Do whatever cleanup.
        self.release()
        if any((exc_type, exc_value, traceback,)):
            raise (exc_type, exc_value, traceback)


class BackgroundMonitor(object):
    # If we will need multiple monitoring contexts (i.e. subprocesses) this will become a dict
    _main_process = None
    _parent_pid = None
    _sub_process_started = None
    _instances = []  # type: List[BackgroundMonitor]

    def __init__(self, wait_period):
        self._event = TrEvent()
        self._done_ev = TrEvent()
        self._start_ev = TrEvent()
        self._task_pid = os.getpid()
        self._thread = None
        self._wait_timeout = wait_period
        self._subprocess = None

    def start(self):
        if not self._thread:
            self._thread = True
        self._event.clear()
        self._done_ev.clear()
        # append to instances
        if self not in BackgroundMonitor._instances:
            stdout_print('ADD bkg service', self, os.getpid())
            BackgroundMonitor._instances.append(self)

    def wait(self, timeout=None):
        self._done_ev.wait(timeout=timeout)

    def _start(self):
        self._thread = Thread(target=self._daemon)
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        if not self._thread:
            return

        if self.is_subprocess_alive():
            self._event.set()

        if isinstance(self._thread, Thread):
            try:
                self._instances.remove(self)
            except ValueError:
                pass
            self._thread = None

    def daemon(self):
        while True:
            if self._event.wait(self._wait_timeout):
                break
            self._daemon_step()

    def _daemon(self):
        self._start_ev.set()
        self.daemon()
        self.post_execution()

    def post_execution(self):
        self._done_ev.set()

    def set_subprocess_mode(self):
        # called just before launching the daemon in a subprocess
        self._subprocess = True
        self._done_ev = PrEvent()
        self._start_ev = PrEvent()
        self._event = PrEvent()

    def _daemon_step(self):
        pass

    @classmethod
    def start_all(cls, execute_in_subprocess, wait_for_subprocess=False):
        if not execute_in_subprocess:
            for d in BackgroundMonitor._instances:
                d._start()
        elif not BackgroundMonitor._main_process:
            cls._parent_pid = os.getpid()
            cls._sub_process_started = PrEvent()
            cls._sub_process_started.clear()
            # setup
            for d in BackgroundMonitor._instances:
                d.set_subprocess_mode()
            BackgroundMonitor._main_process = Process(target=cls._background_process_start)
            BackgroundMonitor._main_process.daemon = True
            BackgroundMonitor._main_process.start()
            # wait until subprocess is up
            if wait_for_subprocess:
                cls._sub_process_started.wait()

    @classmethod
    def _background_process_start(cls):
        is_debugger_running = bool(getattr(sys, 'gettrace', None) and sys.gettrace())
        # restore original signal, this will prevent any deadlocks
        # Do not change the exception we need to catch base exception as well
        stdout_print('Start _background_process_start', os.getpid())
        # noinspection PyBroadException
        try:
            from ... import Task
            # noinspection PyProtectedMember
            Task.current_task()._remove_at_exit_callbacks()
        except:  # noqa
            stdout_print('__register_at_exit failed')

        stdout_print('Start _background_process_start 1 ')

        # if a debugger is running, wait for it to attach to the subprocess
        if is_debugger_running:
            sleep(3)

        stdout_print('Start _background_process_start 2 ')

        # launch all the threads
        for d in cls._instances:
            stdout_print('LAUNCHING {}'.format(d))
            d._start()

        if cls._sub_process_started:
            cls._sub_process_started.set()

        stdout_print('Start _background_process_start 3 ')

        # wait until we are signaled
        for i in BackgroundMonitor._instances:
            # noinspection PyBroadException
            try:
                if i._thread and i._thread.is_alive():
                    # DO Not change, we need to catch base exception, if the process gte's killed
                    try:
                        i._thread.join()
                    except:  # noqa
                        stdout_print('Killed', i)
                        break
                else:
                    stdout_print('Skipping', i)
            except:  # noqa
                stdout_print('FAILED ')
        # we are done, leave process
        stdout_print('Done monitoring', os.getpid())
        return

    def is_alive(self):
        if self.is_subprocess():
            return self.is_subprocess_alive() and self._start_ev.is_set() and not self._done_ev.is_set()
        else:
            return isinstance(self._thread, Thread) and self._thread.is_alive()

    @classmethod
    def is_subprocess_alive(cls):
        if not cls._main_process:
            return False
        # noinspection PyBroadException
        try:
            return \
                cls._main_process.is_alive() and \
                psutil.Process(cls._main_process.pid).status() != psutil.STATUS_ZOMBIE
        except Exception:
            current_pid = cls._main_process.pid
            try:
                parent = psutil.Process(cls._parent_pid)
            except psutil.Error:
                # could not find parent process id
                return
            for child in parent.children(recursive=True):
                # kill ourselves last (if we need to)
                if child.pid == current_pid:
                    return child.status() != psutil.STATUS_ZOMBIE
            return False

    @classmethod
    def is_subprocess(cls):
        return bool(cls._main_process)
