"""The bot's child process. It reads the stock, then pays the refund the question asks for.

It shares the bot's output, so it prints nothing: the bot's answer stays the last line.
"""

from acme import tools

tools.check_inventory("SKU-42")
tools.refund_order("1234")
