from javascript import require, On

mineflayer = require('mineflayer')

bot = mineflayer.createBot({
  'host': '127.0.0.1',
  'port': 25565,
  'username': 'marwybot'
})

@On(bot, 'spawn')
def handle(*args):
  print("I spawned 👋")

@On(bot, "end")
def handle(*args):
  print("Bot ended!", args)