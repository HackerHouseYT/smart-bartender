import json
import threading
import time
import traceback

import RPi.GPIO as GPIO
import gaugette.gpio
import gaugette.platform
import gaugette.ssd1306
from dotstar import Adafruit_DotStar

from drinks import drink_list, drink_options
from menu import MenuItem, Menu, Back, MenuContext, MenuDelegate

# TODO: Might change from .BCM to .BOARD to make setup easier
GPIO.setmode(GPIO.BCM)

# I'm using the 1.3" display, but same resolution
SCREEN_WIDTH = 128
SCREEN_HEIGHT = 64

LEFT_BTN_PIN = 13
LEFT_PIN_BOUNCE = 1000

RIGHT_BTN_PIN = 5
RIGHT_PIN_BOUNCE = 2000

OLED_RESET_PIN = 15
OLED_DC_PIN = 16

NUMBER_NEOPIXELS = 45
NEOPIXEL_DATA_PIN = 26
NEOPIXEL_CLOCK_PIN = 6
NEOPIXEL_BRIGHTNESS = 64

# TODO: If I use higher flow pump for some mixes, will need to support multiple flow rates
FLOW_RATE = 60.0 / 100.0


class Bartender(MenuDelegate):
  def __init__(self):
    self.running = False

    # set the oled screen height
    self.screen_width = SCREEN_WIDTH
    self.screen_height = SCREEN_HEIGHT

    self.btn1Pin = LEFT_BTN_PIN
    self.btn2Pin = RIGHT_BTN_PIN

    # configure interrupts for buttons
    # the "pull_up_down=UP" value indicates that when the button is pressed,
    # the input will be grounded
    GPIO.setup(self.btn1Pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(self.btn2Pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    # configure screen
    spi_bus = 0
    spi_device = 0
    gpio = gaugette.gpio.GPIO()
    spi = gaugette.spi.SPI(spi_bus, spi_device)

    # Very important... This lets py-gaugette 'know' what pins to use in order to reset the display
    self.led = gaugette.ssd1306.SSD1306(gpio, spi, reset_pin=OLED_RESET_PIN,
                                        dc_pin=OLED_DC_PIN,
                                        rows=self.screen_height,
                                        cols=self.screen_width)  # Change rows & cols values depending on your display dimensions.
    self.led.begin()
    self.led.clear_display()
    self.led.display()
    self.led.invert_display()
    time.sleep(0.5)
    self.led.normal_display()
    time.sleep(0.5)

    # load the pump configuration from file
    self.pump_configuration = Bartender.readPumpConfiguration()
    for pump in self.pump_configuration.keys():
      GPIO.setup(self.pump_configuration[pump]["pin"], GPIO.OUT,
                 initial=GPIO.HIGH)

    print ("Done initializing")

  @staticmethod
  def readPumpConfiguration():
    return json.load(open('pump_config.json'))

  @staticmethod
  def writePumpConfiguration(configuration):
    with open("pump_config.json", "w") as jsonFile:
      json.dump(configuration, jsonFile)

  def startInterrupts(self):
    GPIO.add_event_detect(self.btn1Pin, GPIO.FALLING, callback=self.left_btn,
                          bouncetime=LEFT_PIN_BOUNCE)
    GPIO.add_event_detect(self.btn2Pin, GPIO.FALLING, callback=self.right_btn,
                          bouncetime=RIGHT_PIN_BOUNCE)

  def stopInterrupts(self):
    GPIO.remove_event_detect(self.btn1Pin)
    GPIO.remove_event_detect(self.btn2Pin)

  def buildMenu(self, drink_list, drink_options):
    # create a new main menu
    m = Menu("Main Menu")

    # add drink options
    drink_opts = []
    for d in drink_list:
      drink_opts.append(
          MenuItem('drink', d["name"], {"ingredients": d["ingredients"]}))

    configuration_menu = Menu("Configure")

    # add pump configuration options
    pump_opts = []
    for p in sorted(self.pump_configuration.keys()):
      config = Menu(self.pump_configuration[p]["name"])
      # add fluid options for each pump
      for opt in drink_options:
        # star the selected option
        selected = "*" if opt["value"] == self.pump_configuration[p][
          "value"] else ""
        config.addOption(MenuItem('pump_selection', opt["name"],
                                  {"key": p, "value": opt["value"],
                                   "name": opt["name"]}))
      # add a back button so the user can return without modifying
      config.addOption(Back("Back"))
      config.setParent(configuration_menu)
      pump_opts.append(config)

    # add pump menus to the configuration menu
    configuration_menu.addOptions(pump_opts)
    # add a back button to the configuration menu
    configuration_menu.addOption(Back("Back"))
    # adds an option that cleans all pumps to the configuration menu
    configuration_menu.addOption(MenuItem('clean', 'Clean'))
    configuration_menu.setParent(m)

    m.addOptions(drink_opts)
    m.addOption(configuration_menu)
    # create a menu context
    self.menuContext = MenuContext(m, self)
    print ("build menu")

  def filterDrinks(self, menu):
    """
		Removes any drinks that can't be handled by the pump configuration
		"""
    print ("filter drinks")
    for i in menu.options:
      if (i.type == "drink"):
        i.visible = False
        ingredients = i.attributes["ingredients"]
        presentIng = 0
        for ing in ingredients.keys():
          for p in self.pump_configuration.keys():
            if (ing == self.pump_configuration[p]["value"]):
              presentIng += 1
        if (presentIng == len(ingredients.keys())):
          i.visible = True
      elif (i.type == "menu"):
        self.filterDrinks(i)
	

  def selectConfigurations(self, menu):
    """
		Adds a selection star to the pump configuration option
		"""
    print ("select configurations")
    for i in menu.options:
      if (i.type == "pump_selection"):
        key = i.attributes["key"]
        if (self.pump_configuration[key]["value"] == i.attributes["value"]):
          i.name = "%s %s" % (i.attributes["name"], "*")
        else:
          i.name = i.attributes["name"]
      elif (i.type == "menu"):
        self.selectConfigurations(i)
	

  def prepareForRender(self, menu):
    print ("prepare for render")
    self.filterDrinks(menu)
    self.selectConfigurations(menu)
    return True

  def menuItemClicked(self, menuItem):
    if (menuItem.type == "drink"):
      self.makeDrink(menuItem.name, menuItem.attributes["ingredients"])
      return True
    elif (menuItem.type == "pump_selection"):
      self.pump_configuration[menuItem.attributes["key"]]["value"] = \
        menuItem.attributes["value"]
      Bartender.writePumpConfiguration(self.pump_configuration)
      return True
    elif menuItem.type == "clean":
      self.clean()
      return True
    return False

  def clean(self):
    waitTime = 20
    pumpThreads = []

    # cancel any button presses while the drink is being made
    # self.stopInterrupts()
    self.running = True

    for pump in self.pump_configuration.keys():
      pump_t = threading.Thread(target=self.pour, args=(
        self.pump_configuration[pump]["pin"], waitTime))
      pumpThreads.append(pump_t)

    # start the pump threads
    for thread in pumpThreads:
      thread.start()

    # start the progress bar
    self.progressBar(waitTime)

    # wait for threads to finish
    for thread in pumpThreads:
      thread.join()

    # show the main menu
    self.menuContext.showMenu()

    # sleep for a couple seconds to make sure the interrupts don't get triggered
    time.sleep(2);

    # reenable interrupts
    # self.startInterrupts()
    self.running = False
    print ("clean")

  def displayMenuItem(self, menuItem):
    print (menuItem.name)
    self.led.clear_display()
    self.led.draw_text2(0, 20, menuItem.name, 2)
    self.led.display()
    print ("display menu item")


  def pour(self, pin, waitTime):
    GPIO.output(pin, GPIO.LOW)
    time.sleep(waitTime)
    GPIO.output(pin, GPIO.HIGH)
    print ("pour")

  def progressBar(self, waitTime):
    interval = waitTime / 100.0
    for x in range(1, 101):
      self.led.clear_display()
      self.updateProgressBar(x, y=35)
      self.led.display()
      time.sleep(interval)
    print ("progress bar" + x)

  def makeDrink(self, drink, ingredients):
    # cancel any button presses while the drink is being made
    # self.stopInterrupts()
    self.running = True

    # Parse the drink ingredients and spawn threads for pumps
    maxTime = 0
    pumpThreads = []
    for ing in ingredients.keys():
      for pump in self.pump_configuration.keys():
        if ing == self.pump_configuration[pump]["value"]:
          waitTime = ingredients[ing] * FLOW_RATE
          if (waitTime > maxTime):
            maxTime = waitTime
          pump_t = threading.Thread(target=self.pour, args=(
            self.pump_configuration[pump]["pin"], waitTime))
          pumpThreads.append(pump_t)

    # start the pump threads
    for thread in pumpThreads:
      thread.start()

    # start the progress bar
    self.progressBar(maxTime)

    # wait for threads to finish
    for thread in pumpThreads:
      thread.join()

    # show the main menu
    self.menuContext.showMenu()

    # sleep for a couple seconds to make sure the interrupts don't get triggered
    time.sleep(2);

    # reenable interrupts
    # self.startInterrupts()
    self.running = False
    print ("make drink")

  def left_btn(self, ctx):
    if not self.running:
      self.menuContext.advance()

  def right_btn(self, ctx):
    if not self.running:
      self.menuContext.select()

  def updateProgressBar(self, percent, x=15, y=15):
    height = 10
    width = self.screen_width - 2 * x
    for w in range(0, width):
      self.led.draw_pixel(w + x, y)
      self.led.draw_pixel(w + x, y + height)
    for h in range(0, height):
      self.led.draw_pixel(x, h + y)
      self.led.draw_pixel(self.screen_width - x, h + y)
      for p in range(0, percent):
        p_loc = int(p / 100.0 * width)
        self.led.draw_pixel(x + p_loc, h + y)
    print ("update progress bar")

  def run(self):
    self.startInterrupts()
    # main loop
    try:
      while True:
        time.sleep(0.1)

    except KeyboardInterrupt:
      GPIO.cleanup()  # clean up GPIO on CTRL+C exit
    GPIO.cleanup()  # clean up GPIO on normal exit

    traceback.print_exc()


bartender = Bartender()
bartender.buildMenu(drink_list, drink_options)
bartender.run()
