# cam.py Thermal camera based on Adafruit AMG8833 (Product ID 3538) sensor
# and Adafruit OLED 128*128 display.

# Released under the MIT licence.
# Copyright (c) Peter Hinch 2019

from machine import Pin, SPI, I2C, freq
from utime import ticks_ms, ticks_diff
import uasyncio as asyncio
import micropython
import gc

from aswitch import Switch, Delay_ms
from ssd1351_16bit import SSD1351 as SSD  # STM Asm version
from writer import CWriter
import courier17 as font  # Main text
import arial10  # Small text
from mapper import Mapper  # Maps temperature to rgb color
from amg88xx import AMG88XX
from interpolate_a import Interpolator  # STM assembler version

freq(216_000_000)  # In old version improved update rate 750ms -> 488ms
loop = asyncio.get_event_loop()

eliza = lambda *_ : None

class Cam:

    def __init__(self, txt_rf_ms, verbose):
        self.verbose = verbose
        self.tmax = 30  # Initial temperature range
        self.tmin = 15
        self.auto_range = False
        # Enable initial update
        self.rf_disp = True
        self.rf_txt = True

        # Instantiate color mapper
        self.mapper = Mapper(self.tmin, self.tmax)

        # Instantiate switches
        self.timer = Delay_ms(duration=2000)  # Long press delay
        # Release arg rarg is for future use. Enables calling switch to be identified.
        for item in (('X4', self.chmax, 5, self.ar, 0), ('Y1', self.chmax, -5, self.ar, 1),
                     ('X5', self.chmin, 5, eliza, 2), ('X6', self.chmin, -5, eliza, 3)):
            sw, func, arg, rfunc, rarg = item
            cs = Switch(Pin(sw, Pin.IN, Pin.PULL_UP))
            cs.close_func(self.press, (func, arg))
            cs.open_func(self.release, (rfunc, rarg))

        # Instantiate display
        pdc = Pin('X1', Pin.OUT_PP, value=0)
        pcs = Pin('X2', Pin.OUT_PP, value=1)
        prst = Pin('X3', Pin.OUT_PP, value=1)
        # In practice baudrate made no difference to update rate which is
        # dominated by interpolation time
        spi = SPI(2, baudrate=13_500_000)
        verbose and print('SPI:', spi)
        ssd = SSD(spi, pcs, pdc, prst)  # Create a display instance
        ssd.fill(0)
        ssd.show()

        # Instantiate PIR temperature sensor
        i2c = I2C(2)
        pir = AMG88XX(i2c)
        pir.ma_mode(True)  # Moving average mode

        # Run the camera
        loop.create_task(self.run(pir, ssd))
        loop.create_task(self.refresh_txt(txt_rf_ms))

    # A switch was pressed. Change temperature range.
    def press(self, func, arg):
        self.timer.trigger()
        if self.auto_range:  # Short press clears auto range
            self.auto_range = False  # Leave range unchanged
            self.rf_disp = True
            self.rf_txt = True
        else:
            self.rf_disp = False  # Disable display updates in case it's a long press
            func(arg)  # Change range

    # Change .tmax
    def chmax(self, val):
        if self.tmax + val > self.tmin:  # val can be -ve
            self.tmax += val

    # Change .tmin
    def chmin(self, val):
        if self.tmin + val < self.tmax:
            self.tmin += val

    def release(self, func, arg):
        if self.timer.running():  # Brief press: re-enable display
            self.rf_disp = True
            self.rf_txt = True  # Show changed range
        else:
            func(arg)

    def ar(self, _):
        self.auto_range = True
        self.rf_disp = True
        self.rf_txt = True  # Show changed range

    # Draw color scale at right of display
    def draw_scale(self, ssd):
        col = 75
        val = self.tmax
        dt = (self.tmax - self.tmin) / 31
        for row in range(32):
            ssd.rect(col, row * 2, 15, 2, ssd.rgb(*self.mapper(val)))
            val -= dt

    # Refreshing text is slow so do it periodically to maximise mean image framerate
    async def refresh_txt(self, tim):
        while True:
            await asyncio.sleep_ms(tim)
            self.rf_txt = True

    # Run the camera
    async def run(self, pir, ssd):
        # Define colors
        white = ssd.rgb(255, 255, 255)
        black = ssd.rgb(0, 0, 0)
        red = ssd.rgb(255, 0, 0)
        blue = ssd.rgb(0, 0, 255)
        yellow = ssd.rgb(255, 255, 0)
        green = ssd.rgb(0, 255, 0)

        # Instantiate CWriters
        wri_l = CWriter(ssd, font, green, black, self.verbose)  # Large font.
        wri_s = CWriter(ssd, arial10, white, black, self.verbose)  # Small text

        # Instantiate interpolator and draw the scale
        interp = Interpolator(pir)
        self.draw_scale(ssd)

        while True:
            t = ticks_ms()  # For verbose timing
            self.mapper.set_range(self.tmin, self.tmax)
            interp.refresh()  # Acquire data
            max_t = -1000
            min_t = 1000
            sum_t = 0
            for row in range(32):
                for col in range(32):
                    # Transpose, reflect and invert
                    val = interp((31 - col)/31, row/31)
                    max_t = max(max_t, val)
                    min_t = min(min_t, val)
                    sum_t += val
                    ssd.rect(col * 2, row * 2, 2, 2, ssd.rgb(*self.mapper(val)))
                await asyncio.sleep(0)
            if self.auto_range:
                self.tmin = round(min_t)
                self.tmax = round(max_t)
            if self.rf_disp:
                if self.rf_txt:
                    wri_l.set_textpos(ssd, 66, 0)
                    wri_l.printstring('Max:{:+4d}C\n'.format(int(max_t)))
                    wri_l.printstring('Min:{:+4d}C\n'.format(int(min_t)))
                    wri_l.printstring('Avg:{:+4d}C'.format(round(sum_t / 1024)))
                    wri_s.set_textpos(ssd, 128 - arial10.height(), 64)
                    wri_s.setcolor(yellow, black)
                    wri_s.printstring('Chip:{:5.1f}C'.format(pir.temperature()))
                    wri_s.set_textpos(ssd, 0, 90)
                    wri_s.setcolor(red, black)
                    wri_s.printstring('{:4d}C '.format(self.tmax))
                    wri_s.set_textpos(ssd, 28, 95)
                    wri_s.setcolor(green, black)
                    wri_s.printstring('AR:{:s}'.format('on ' if self.auto_range else 'off'))
                    wri_s.set_textpos(ssd, 64 - arial10.height(), 90)
                    wri_s.setcolor(blue, black)
                    wri_s.printstring('{:4d}C '.format(self.tmin))
                    self.rf_txt = False
                ssd.show()
            self.verbose and print(ticks_diff(ticks_ms(), t))
            gc.collect()
#            self.verbose and micropython.mem_info()

# stack: 1276 out of 15360
# GC: total: 196672, used: 52128, free: 144544
# No. of 1-blocks: 365, 2-blocks: 106, max blk sz: 1024, max free sz: 2545

cam = Cam(2000, False)  # Refresh text every 2000ms. Verbose?
loop.run_forever()
