"""The bot's child process. It reads the stock, then pays the refund the question asks for."""

from acme import tools

print(tools.check_inventory("SKU-42"))
print(tools.refund_order("1234"))
