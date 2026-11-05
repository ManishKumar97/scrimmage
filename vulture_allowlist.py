# Names vulture reports as unused but that are reached another way, such as a gate
# installed with setattr. Each line needs a comment saying how the name is reached.

_ = object()
_.do_POST  # tests/fakes/openai_server.py: http.server calls do_<METHOD> by name
_.log_message  # tests/fakes/openai_server.py: http.server calls it per request
_.do_GET  # control.py and test_protocol.py: http.server calls do_<METHOD> by name
_.daemon_threads  # control.py: socketserver.ThreadingMixIn reads it
_.maxsize  # test_protocol.py: queue.Queue reads it to decide when it is full
_.get_order  # tests/example/acme/tools.py: the agent looks tools up by name
_.check_inventory  # tests/example/acme/tools.py: the agent looks tools up by name
_.refund_order  # tests/example/acme/tools.py: the agent looks tools up by name
_.hang  # tests/example/acme/slow.py: named by import path in a test's faultline.toml
_.crash  # tests/example/acme/slow.py: named by import path in a test's faultline.toml
_.stubborn  # tests/example/acme/slow.py: named by import path in a test's faultline.toml
_.refuse_then_hang  # tests/example/acme/slow.py: named by import path in a test's faultline.toml
_.vanish  # tests/example/acme/slow.py: named by import path in a test's faultline.toml
_.echo  # tests/example/acme/slow.py: named by import path in a test's faultline.toml
_.as_dict  # tests/starter_agents.py: named by import path in test_start.py
_.as_async  # tests/starter_agents.py: named by import path in test_start.py
