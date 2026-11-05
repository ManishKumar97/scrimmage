# Names vulture reports as unused but that are reached another way, such as a gate
# installed with setattr. Each line needs a comment saying how the name is reached.

from fakes.openai_server import FakeOpenAI

handler = FakeOpenAI([])._handler()
handler.do_POST  # http.server calls do_<METHOD> by name
handler.log_message  # http.server calls it for every request
