# Names vulture reports as unused but that are reached another way, such as a gate
# installed with setattr. Each line needs a comment saying how the name is reached.

_ = object()
_.do_POST  # tests/fakes/openai_server.py: http.server calls do_<METHOD> by name
_.log_message  # tests/fakes/openai_server.py: http.server calls it per request
