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
_.pytest_sessionfinish  # tests/conftest.py: pytest calls hooks by name
_.exitstatus  # tests/conftest.py: pytest reads it to set the exit code
_.answer_streamed  # tests/example/acme/rag.py: named by import path in a test's faultline.toml
_.answer_priceless  # tests/example/acme/rag.py: named by import path in a test's faultline.toml
_.answer_twice  # tests/example/acme/rag.py: named by import path in a test's faultline.toml
_.answer_tuning  # tests/example/acme/rag.py: named by import path in a test's faultline.toml
_.answer_hosted  # tests/example/acme/rag.py: named by import path in a test's faultline.toml
_.answer_odd_tool  # tests/example/acme/rag.py: named by import path in a test's faultline.toml
_.answer_unseen  # tests/example/acme/rag.py: named by import path in a test's faultline.toml
_.answer_short  # tests/example/acme/rag.py: named by import path in a test's faultline.toml
_.answer_if_asked  # tests/example/acme/rag.py: named by import path in a test's faultline.toml
_.answer_web_search  # tests/example/acme/rag.py: named by import path in a test's faultline.toml
_.answer_old_functions  # tests/example/acme/rag.py: named by import path in a test's faultline.toml
_.answer_renamed  # tests/example/acme/rag.py: named by import path in a test's faultline.toml
_.answer_renamed_responses  # tests/example/acme/rag.py: named by import path in a test
_.use_httpx2  # tests/example/acme/slow.py: named by import path in a test's faultline.toml
_.answer_one_stops_the_rest  # tests/example/acme/rag.py: named by import path in a test
_.answer_one_stops_one_finishes  # tests/example/acme/rag.py: named by import path in a test
