#!/usr/bin/env python

'''Precise framerate calculation, scheduling and framerate limiting.

Measuring time
==============

    >>> from pylet import clock
    >>> while True:
    ...     dt = clock.tick()
    ...     # (update, render)
    ...     print 'FPS is %f' % clock.get_fps()
    >>>

The `dt` value returned gives the number of seconds (as a float) since the
last "tick".

The `get_fps` function averages the framerate over a sliding window of
approximately 1 second.  (You can calculate the instantaneous framerate by
taking the reciprocal of `dt`).

Always remember to `tick` the clock!

Limiting frame-rate
===================

The framerate can be limited::

    >>> clock.set_fps_limit(60)
    >>>

This causes `clock` to sleep during each `tick` in an attempt to keep the
number of ticks (frames) per second below 60.

The implementation uses platform-dependent high-resolution sleep functions
to achieve better accuracy with busy-waiting than would be possible using
just the `time` module.  

Scheduling
==========

You can schedule a function to be called every time the clock is ticked::

    >>> def callback(dt):
    ...     print '%f seconds since last callback' % dt
    ...
    >>> clock.schedule(callback)
    >>>

The `schedule_interval` method causes a function to be called every `n`
seconds::

    >>> clock.schedule_interval(callback, .5)   # called twice a second
    >>>

The `schedule_once` method causes a function to be called once `n` seconds
in the future::

    >>> clock.schedule_once(callback, 5)        # called in 5 seconds
    >>>

All of the `schedule` methods will pass on any additional args or keyword args
you specify ot the callback function::

    >>> def animate(dt, velocity, sprite):
    ...    sprite.position += dt * velocity
    ...
    >>> clock.schedule(animate, velocity=5.0, sprite=alien)
    >>>

You can cancel a function scheduled with any of these methods using
`unschedule`::

    >>> clock.unschedule(animate)
    >>>

Displaying FPS
==============

The ClockDisplay class provides a simple FPS counter::

    >>> fps_display = clock.ClockDisplay()
    >>> fps_display.draw()
    >>>

There are several options to change the font, color and text displayed
within the __init__ method.  The display will be bottom-right aligned
to the window by default (XXX temporary).

Using multiple clocks
=====================

The clock functions are all relayed to an instance of `Clock` which is
initalised with the module.  You can get this instance to use directly::

    >>> clk = clock.get_default()
    >>>

You can also replace the default clock with your own:

    >>> myclk = clock.Clock()
    >>> clock.set_default(myclk)

Each clock maintains its own set of scheduled functions and FPS
limiting/measurement.  Each clock must be `tick`d separately.

Multiple and derived clocks potentially allow you to separate "game-time" and
"wall-time", or to synchronise your clock to an audio or video stream instead
of the system clock.

'''

__docformat__ = 'restructuredtext'
__version__ = '$Id$'

import time
import sys
import operator
import ctypes
import ctypes.util

if sys.platform in ('win32', 'cygwin'):
    # Win32 Sleep function is only 10-millisecond resolution, so instead
    # use a waitable timer object, which has up to 100-nanosecond resolution
    # (hardware and implementation dependent, of course).
    _kernel32 = ctypes.windll.kernel32
    class _ClockBase(object):
        def __init__(self):
            self._timer = _kernel32.CreateWaitableTimerA(ctypes.c_void_p(), 
                True, ctypes.c_void_p())

        def sleep(self, microseconds):
            delay = ctypes.c_longlong(int(-microseconds * 10))
            _kernel32.SetWaitableTimer(self._timer, ctypes.byref(delay), 
                0, ctypes.c_void_p(), ctypes.c_void_p(), False)
            _kernel32.WaitForSingleObject(self._timer, 0xffffffff)

else:
    path = ctypes.util.find_library('c')
    if not path:
        raise ImportError('libc not found')
    _c = ctypes.cdll.LoadLibrary(path)
    _c.usleep.argtypes = [ctypes.c_ulong]

    class _ClockBase(object):
        def sleep(self, microseconds):
            _c.usleep(int(microseconds))

class ScheduledIntervalItem(object):
    __slots__ = ['func', 'interval', 'last_ts', 'next_ts', 
                 'args', 'kwargs']
    def __init__(self, func, interval, last_ts, next_ts, args, kwargs):
        self.func = func
        self.interval = interval
        self.last_ts = last_ts
        self.next_ts = next_ts
        self.args = args
        self.kwargs = kwargs

class Clock(_ClockBase):
    '''Class for calculating and limiting framerate, and for calling scheduled
    functions.
    '''

    # No attempt to sleep will be made for less than this time.  Setting
    # high will increase accuracy and CPU burn.  Setting low reduces accuracy
    # but ensures more sleeping takes place rather than busy-loop.
    MIN_SLEEP = 0.005

    # Sleep by the desired amount minus this bit.  This is to compensate
    # for operating systems being a bit lazy in returning control.
    SLEEP_UNDERSHOOT = MIN_SLEEP - 0.001

    # List of functions to call every tick.
    schedule_items = None

    # List of schedule interval items kept in sort order.
    schedule_interval_items = None

    # Dict mapping function to schedule item for fast removal 
    schedule_functions = None

    def __init__(self, fps_limit=None, time_function=time.time):
        '''Initialise a Clock, with optional framerate limit and custom
        time function.

        :Parameters:
            `fps_limit` : float
                If not None, the maximum allowable framerate.  Defaults
                to None.
            `time_function` : function
                Function to return the elapsed time of the application, 
                in seconds.  Defaults to time.time, but can be replaced
                to allow for easy time dilation effects or game pausing.

        '''

        super(Clock, self).__init__()
        self.time = time_function
        self.next_ts = self.time()
        self.last_ts = None
        self.times = []

        self.set_fps_limit(fps_limit)
        self.cumulative_time = 0

        self.schedule_items = []
        self.schedule_interval_items = []
        self.schedule_functions = {}

    def tick(self):
        '''Signify that one frame has passed.

        This will call any scheduled functions that have elapsed.

        :return: The number of seconds (as a float) since the last `tick`,
            or 0 if this was the first frame.

        '''
        if self.period_limit:
            self.limit()

        ts = self.time()
        if self.last_ts is None: 
            delta_t = 0
        else:
            delta_t = ts - self.last_ts
            self.times.insert(0, delta_t)
            if len(self.times) > self.window_size:
                self.cumulative_time -= self.times.pop()
        self.cumulative_time += delta_t
        self.last_ts = ts

        # Call functions scheduled for every frame  
        for func, args, kwargs in self.schedule_items:
            func(delta_t, *args, **kwargs)

        # Call all scheduled interval functions and reschedule for future.
        need_resort = False
        for item in self.schedule_interval_items:
            if item.next_ts > ts:
                break
            item.func(ts - item.last_ts, *item.args, **item.kwargs)
            if item.interval:
                item.last_ts = ts
                # Try to keep timing regular, even if overslept this time;
                # but don't schedule in the past (which could lead to
                # infinitely-worsing error).
                item.next_ts = max(item.next_ts + item.interval, ts + 0.01)
                need_resort = True

        # Remove finished one-shots.
        self.schedule_interval_items = \
            [item for item in self.schedule_interval_items \
             if item.next_ts > ts]

        if need_resort:
            # TODO bubble up changed items might be faster
            self.schedule_interval_items.sort(key=lambda a: a.next_ts)

        return delta_t

    def limit(self):
        '''Sleep until the next frame is due.  Called automatically by
        `tick` if a framerate limit has been set.

        This method uses several heuristics to determine whether to
        sleep or busy-wait (or both).
        '''
        ts = self.time()
        # Sleep to just before the desired time
        sleeptime = self.next_ts - self.time()
        while sleeptime - self.SLEEP_UNDERSHOOT > self.MIN_SLEEP:
            self.sleep(1000000 * (sleeptime - self.SLEEP_UNDERSHOOT))
            sleeptime = self.next_ts - self.time()

        # Busy-loop CPU to get closest to the mark
        sleeptime = self.next_ts - self.time()
        while sleeptime > 0:
            sleeptime = self.next_ts - self.time()

        if sleeptime < -2 * self.period_limit:
            # Missed the time by a long shot, let's reset the clock
            # print >> sys.stderr, 'Step %f' % -sleeptime
            self.next_ts = ts + 2 * self.period_limit
        else:
            # Otherwise keep the clock steady
            self.next_ts = self.next_ts + self.period_limit

    def set_fps_limit(self, fps_limit):
        '''Set the framerate limit.

        :Parameters:
            `fps_limit` : float
                Maximum frames per second allowed, or None to disable
                limiting.

        '''
        if not fps_limit:
            self.period_limit = None
        else:
            self.period_limit = 1. / fps_limit
        self.window_size = fps_limit or 60

    def get_fps_limit(self):
        '''Get the framerate limit.

        :return: The framerate limit previously set in the constructor or
            `set_fps_limit`, or None if no limit was set.

        '''
        if self.period_limit:
            return 1. / self.period_limit
        else:
            return 0

    def get_fps(self):
        '''Get the average FPS of recent history, as a float.  The result
        is the average of a sliding window of the last `n` frames, where
        `n` is some number designed to cover approximately 1 second.

        :return: The measured frames per second.
        '''
        if not self.cumulative_time: 
            return 0
        return len(self.times) / self.cumulative_time

    def schedule(self, func, *args, **kwargs):
        '''Schedule a function to be called every frame.

        The function should have a prototype that includes `dt` as the
        first argument, which gives the elapsed time, in seconds, since the
        last clock tick.  Any additional arguments given to this function
        are passed on to the callback::

            def callback(dt, *args, **kwargs):
                pass

        Note that this method is also implemented on Clock, which schedules
        the function on the default clock.
        '''
        item = (func, args, kwargs)
        self.schedule_items.append(item)

        # schedule_functions gives mapping of func to item.  if func
        # is already scheduled, the mapping becomes a list of items.
        if func in self.schedule_functions:
            entry = self.schedule_functions[func]
            if type(entry) == list:
                self.schedule_functions[func].append(item)
            else:
                self.schedule_functions[func] = [entry, item]
        else:
            self.schedule_functions[func] = item

    def schedule_item(self, func, interval, repeat, *args, **kwargs):
        last_ts = self.last_ts or self.next_ts
        next_ts = last_ts + interval
        if not repeat:
            interval = 0

        item = ScheduledIntervalItem(
            func, interval, last_ts, next_ts, args, kwargs)

        # Insert in sort order
        for i, other in enumerate(self.schedule_interval_items):
            if other.next_ts > next_ts:
                self.schedule_interval_items.insert(i, item)
                return
        self.schedule_interval_items.append(item)

        # add item to func mapping
        if func in self.schedule_functions:
            entry = self.schedule_functions[func]
            if type(entry) == list:
                self.schedule_functions[func].append(item)
            else:
                self.schedule_functions[func] = [entry, item]
        else:
            self.schedule_functions[func] = item

    def schedule_interval(self, func, interval, *args, **kwargs):
        '''Schedule a function to be called every `interval` seconds.

        Specifying an interval of 0 prevents the function from being
        called again (see `schedule` to call a function as often as possible).

        The callback function prototype is the same as for `schedule`.

        Note that this method is also implemented on Clock, which schedules
        the function on the default clock.
        '''
        self.schedule_item(func, interval, True, *args, **kwargs)

    def schedule_once(self, func, delay, *args, **kwargs):
        '''Schedule a function to be called once after `delay` seconds.

        The callback function prototype is the same as for `schedule`.

        Note that this method is also implemented on Clock, which schedules
        the function on the default clock.
        '''
        self.schedule_item(func, delay, False, *args, **kwargs)

    def unschedule(self, func):
        '''Remove a function from the schedule.  If it appears in the
        schedule more than once, all occurances are removed.  If the
        function was not scheduled, no error is raised.
        '''
        if func not in self.schedule_functions:
            return

        items = self.schedule_functions[func]
        if type(items) == list:
            for item in items:
                if item in self.schedule_items:
                    self.schedule_items.remove(item)
                elif item in self.schedule_interval_items:
                    self.schedule_interval_items.remove(item)
        else:
            if items in self.schedule_items:
                self.schedule_items.remove(items)
            elif item in self.schedule_interval_items:
                self.schedule_interval_items.remove(item)
        del self.schedule_functions[func]


# Default clock.
_default = Clock()

def set_default(default):
    '''Set the default clock to use for all module-level functions.

    By default an instance of `Clock` is used.
    '''
    global _default
    _default = default

def get_default():
    '''Return the `Clock` instance that is used by all module-level
    clock functions.
    '''
    return _default

def tick():
    '''Signify that one frame has passed on the default clock.

    This will call any scheduled functions that have elapsed.

    :return: The number of seconds (as a float) since the last `tick`,
        or 0 if this was the first frame.
    '''

    return _default.tick()

def get_fps():
    '''Return the current measured FPS of the default clock.
    '''
    return _default.get_fps()

def set_fps_limit(fps_limit):
    '''Set the framerate limit for the default clock.

    :Parameters:
        `fps_limit` : float
            Maximum frames per second allowed, or None to disable
            limiting.

    '''
    _default.set_fps_limit(fps_limit)

def get_fps_limit():
    '''Get the framerate limit for the default clock.

    :return: The framerate limit previously set by `set_fps_limit`, or None if
        no limit was set.

    '''
    return _default.get_fps_limit()

def schedule(func, *args, **kwargs):
    '''Schedule 'func' to be called every frame on the default clock.  The
    arguments passed to func are 'dt', followed by any *args and **kwargs
    given here.

    If no default clock is set, the func is queued and will be scheduled
    on the default clock as soon as it is created.
    '''
    _default.schedule(func, *args, **kwargs)

def schedule_interval(func, interval, *args, **kwargs):
    '''Schedule 'func' to be called every 'interval' seconds (can be
    a float) on the default clock.  The arguments passed to 'func' are
    'dt' (time since last function call), followed by any *args and
    **kwargs given here.
    
    If no default clock is set, the func is queued and will be scheduled
    on the default clock as soon as it is created.
    '''
    _default.schedule_interval(func, interval, *args, **kwargs)

def schedule_once(func, delay, *args, **kwargs):
    '''Schedule 'func' to be called once after 'delay' seconds (can be
    a float) on the default clock.  The arguments passed to 'func' are
    'dt' (time since last function call), followed by any *args and
    **kwargs given here.
    
    If no default clock is set, the func is queued and will be scheduled
    on the default clock as soon as it is created.
    ''' 
    _default.schedule_once(func, delay, *args, **kwargs)

def unschedule(func):
    '''Remove 'func' from the default clock's schedule.  No error
    is raised if the func was never scheduled.
    '''
    _default.unschedule(func)

class ClockDisplay(object):
    '''Display current clock values, such as FPS.

    Assumes an orthogonal window projection.
    '''
    
    window_width = 0 #XXX temp
    def __init__(self, 
                 font=None,
                 interval=0.25,
                 format='%(fps).2f',
                 color=(.5, .5, .5, .5),
                 clock=None):

        if clock is None:
            self.clock = _default
        self.clock.schedule_interval(self.update_text, interval)

        if not font:
            from pyglet.font import load as load_font
            font = load_font('', 36, bold=True)

        import pyglet.font
        self.label = pyglet.font.Label(font, '', color=color, x=10, y=10)

        self.format = format

    def update_text(self, dt=0):
        fps = self.clock.get_fps()
        self.label.text = self.format % {'fps': fps}

    def draw(self):
        self.label.draw()

    def on_resize(self, width, height):
        # TODO when TextSprite implements Sprite.
        #self.sprite.right = width
        self.window_width = width

if __name__ == '__main__':
    import sys
    import getopt
    test_seconds = 1 
    test_fps = 60
    show_fps = False
    options, args = getopt.getopt(sys.argv[1:], 'vht:f:', 
        ['time=', 'fps=', 'help'])
    for key, value in options:
        if key in ('-t', '--time'):
            test_seconds = float(value)
        elif key in ('-f', '--fps'):
            test_fps = float(value)
        elif key in ('-v'):
            show_fps = True
        elif key in ('-h', '--help'):
            print ('Usage: clock.py <options>\n'
                   '\n'
                   'Options:\n'
                   '  -t   --time       Number of seconds to run for.\n'
                   '  -f   --fps        Target FPS.\n'
                   '\n'
                   'Tests the clock module by measuring how close we can\n'
                   'get to the desired FPS by sleeping and busy-waiting.')
            sys.exit(0) 

    set_fps_limit(test_fps)
    start = time.time()

    # Add one because first frame has no update interval.
    n_frames = int(test_seconds * test_fps + 1)

    print 'Testing %f FPS for %f seconds...' % (test_fps, test_seconds)
    for i in xrange(n_frames):
        tick()
        if show_fps:
            print get_fps()
    total_time = time.time() - start
    total_error = total_time - test_seconds
    print 'Total clock error: %f secs' % total_error
    print 'Total clock error / secs: %f secs/secs' % \
        (total_error / test_seconds)

    # Not fair to add the extra frame in this calc, since no-one's interested
    # in the startup situation.
    print 'Average FPS: %f' % ((n_frames - 1) / total_time)

