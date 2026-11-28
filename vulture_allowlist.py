# Names vulture reports as unused but that are reached another way, such as a gate
# installed with setattr. Each line needs a comment saying how the name is reached.

_ = object()
_.do_POST  # tests/fakes/openai_server.py: http.server calls do_<METHOD> by name
_.connection_made  # tests/fakes/fast_server.py: asyncio calls a Protocol's methods by name
_.data_received  # tests/fakes/fast_server.py: asyncio calls a Protocol's methods by name
_.log_message  # tests/fakes/openai_server.py: http.server calls it per request
_.do_GET  # control.py and test_protocol.py: http.server calls do_<METHOD> by name
_.daemon_threads  # control.py: socketserver.ThreadingMixIn reads it
_.request_queue_size  # control.py: socketserver.TCPServer reads it
_.address_family  # control.py: socketserver.TCPServer reads it to make its socket
_.readable  # control.py: io.BufferedReader asks its raw stream by name
_.auto_open  # tests/test_control_tls.py: http.client reads it before it reconnects
_.maxsize  # test_protocol.py: queue.Queue reads it to decide when it is full
_.get_order  # tests/example/acme/tools.py: the agent looks tools up by name
_.check_inventory  # tests/example/acme/tools.py: the agent looks tools up by name
_.refund_order  # tests/example/acme/tools.py: the agent looks tools up by name
_.hang  # tests/example/acme/slow.py: named by import path in a test's scrimmage.toml
_.crash  # tests/example/acme/slow.py: named by import path in a test's scrimmage.toml
_.stubborn  # tests/example/acme/slow.py: named by import path in a test's scrimmage.toml
_.refuse_then_hang  # tests/example/acme/slow.py: named by import path in a test's scrimmage.toml
_.vanish  # tests/example/acme/slow.py: named by import path in a test's scrimmage.toml
_.echo  # tests/example/acme/slow.py: named by import path in a test's scrimmage.toml
_.as_dict  # tests/starter_agents.py: named by import path in test_start.py
_.as_text_enum  # tests/starter_agents.py: named by import path in test_start.py
_.as_async  # tests/starter_agents.py: named by import path in test_start.py
_.answer_async  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.answer_handled  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.answer_custom_class  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.answer_unseen  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.real_failure  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.real_failure_handled  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.nested  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.sync_tool_through_arun  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.async_tool  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.handle_tool_error  # tests/example/acme/graph.py: LangChain reads it on the tool
_.answer_told  # tests/example/acme/agents_bot.py: a test's scrimmage.toml names it
_.answer_raises  # tests/example/acme/agents_bot.py: a test's scrimmage.toml names it
_.answer_timed  # tests/example/acme/agents_bot.py: a test's scrimmage.toml names it
_.answer_sync  # tests/example/acme/agents_bot.py: a test's scrimmage.toml names it
_.answer_custom  # tests/example/acme/agents_bot.py: a test's scrimmage.toml names it
_.answer_replaced  # tests/example/acme/agents_bot.py: a test's scrimmage.toml names it
_.answer_mcp  # tests/example/acme/agents_bot.py: a test's scrimmage.toml names it
_.answer_impl_replaced  # tests/example/acme/agents_bot.py: a test's scrimmage.toml names it
_._arun  # tests/test_langgraph.py: a stand-in for LangChain's own, read by the structural check
_.mixin_tool  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.answer_overridden  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.report_tool  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.NotAnError  # tests/test_tool_gate.py: named by import path in a fault's raises
_.TwoArguments  # tests/test_tool_gate.py: named by import path in a fault's raises
_.a_function  # tests/test_tool_gate.py: named by import path in a fault's raises
_.plumbing  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.answer_handled_text  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.answer_handled_callable  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.answer_handled_async  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.renamed  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.plain_tool  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.first_goes_quiet  # tests/example/acme/slow.py: a test's scrimmage.toml names it
_.pytest_configure  # tests/conftest.py: pytest calls hooks by name
_.pytest_collection_modifyitems  # tests/conftest.py: pytest calls hooks by name
_.coverage_init  # tools/coverage-threads: coverage.py calls a plugin's init by name
_.pytest_sessionfinish  # tests/conftest.py: pytest calls hooks by name
_.pytest_runtest_makereport  # tests/conftest.py: pytest calls hooks by name
_.pytest_runtest_setup  # tests/conftest.py: pytest calls hooks by name
_.pytest_runtest_call  # tests/conftest.py: pytest calls hooks by name
_.pytest_runtest_teardown  # tests/conftest.py: pytest calls hooks by name
_.pytest_warning_recorded  # tests/conftest.py: pytest calls hooks by name
_.pytest_terminal_summary  # tests/conftest.py: pytest calls hooks by name
_.exitstatus  # tests/conftest.py: pytest reads it to set the exit code
_.answer_streamed  # tests/example/acme/rag.py: named by import path in a test's scrimmage.toml
_.answer_priceless  # tests/example/acme/rag.py: named by import path in a test's scrimmage.toml
_.answer_streamed_slowly  # tests/example/acme/rag.py: named by import path in scrimmage.toml
_.answer_holding_a_stream  # tests/example/acme/rag.py: named by import path in scrimmage.toml
_.answer_killed_holding  # tests/example/acme/rag.py: named in a test's scrimmage.toml
_.answer_many_async  # tests/example/acme/rag.py: named in a test's scrimmage.toml
_.answer_short_twice  # tests/example/acme/rag.py: named by import path in a test's scrimmage.toml
_.answer_web_after_close  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_unknown_request  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_with_reads  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.handbook  # tests/fakes/docs_server.py: the MCP server calls its resources by name
_.summarise  # tests/fakes/docs_server.py: the MCP server calls its prompts by name
_.answer_three_times  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_after_editing_the_first  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_._handle_list_tools  # tests/fakes/docs_server.py: MCPServer hands it to its low-level server
_.answer_slashed_tool  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_two_readings  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_fastmcp_two_servers  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_langchain  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_after_close  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_twice  # tests/example/acme/rag.py: named by import path in a test's scrimmage.toml
_.answer_tuning  # tests/example/acme/rag.py: named by import path in a test's scrimmage.toml
_.answer_hosted  # tests/example/acme/rag.py: named by import path in a test's scrimmage.toml
_.answer_odd_tool  # tests/example/acme/rag.py: named by import path in a test's scrimmage.toml
_.answer_unseen  # tests/example/acme/rag.py: named by import path in a test's scrimmage.toml
_.answer_short  # tests/example/acme/rag.py: named by import path in a test's scrimmage.toml
_.answer_if_asked  # tests/example/acme/rag.py: named by import path in a test's scrimmage.toml
_.answer_web_search  # tests/example/acme/rag.py: named by import path in a test's scrimmage.toml
_.answer_old_functions  # tests/example/acme/rag.py: named by import path in a test's scrimmage.toml
_.answer_renamed  # tests/example/acme/rag.py: named by import path in a test's scrimmage.toml
_.answer_renamed_responses  # tests/example/acme/rag.py: named by import path in a test
_.use_httpx2  # tests/example/acme/slow.py: named by import path in a test's scrimmage.toml
_.invoice_pdf  # tests/example/acme/mock_values.py: named by a mock's returns_from in a test
_.answer_one_stops_the_rest  # tests/example/acme/rag.py: named by import path in a test
_.answer_one_odd_tool_stops_the_rest  # tests/example/acme/rag.py: named by import path in a test
_.answer_one_unseen_stops_the_rest  # tests/example/acme/rag.py: named by import path in a test
_.answer_forked  # tests/example/acme/rag.py: named by import path in a test
_.answer_one_stops_one_finishes  # tests/example/acme/rag.py: named by import path in a test
_.same_name_as_a_gated_tool  # tests/example/acme/unsafe.py: a test's scrimmage.toml names it
_.run_set_on_the_tool  # tests/example/acme/unsafe.py: a test's scrimmage.toml names it
_.run_set_when_made  # tests/example/acme/unsafe.py: a test's scrimmage.toml names it
_.invoke_set_on_the_class_later  # tests/example/acme/unsafe.py: a test's scrimmage.toml names it
_.run_set_on_the_class_later  # tests/example/acme/unsafe.py: a test's scrimmage.toml names it
_.wrapped_gate  # tests/example/acme/unsafe.py: a test's scrimmage.toml names it
_.invoke_override_called_directly  # tests/example/acme/unsafe.py: a test's scrimmage.toml names it
_.run_override_called_directly  # tests/example/acme/unsafe.py: a test's scrimmage.toml names it
_.model_post_init  # tests/example/acme/unsafe.py: pydantic calls it
_.context  # tests/example/acme/unsafe.py: pydantic passes it
_.run_set_on_the_tool_called_directly  # tests/example/acme/unsafe.py: a test names it
_.answer_after_a_child  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.answer_with_runtime  # tests/example/acme/graph.py: a test's scrimmage.toml names it
_.run_manager  # tests/example/acme/graph.py: LangChain passes it to the tool
_.partial_run  # tests/example/acme/unsafe.py: a test's scrimmage.toml names it
_.static_run_and_override  # tests/example/acme/unsafe.py: a test names it
_.post_init_run_and_override  # tests/example/acme/unsafe.py: a test names it
_.ordinary_tool  # tests/example/acme/unsafe.py: a test names it
_.override_named_by_the_test  # tests/example/acme/unsafe.py: a test names it
_.as_tool  # tests/example/acme/unsafe.py: an override the adapter must leave alone
_.override_with_no_init  # tests/example/acme/unsafe.py: a test names it
_.__init_subclass__  # langchain_gate.py and tests: Python calls it
_.answer_with_injected  # tests/example/acme/graph.py: a test names it
_.renamed_copy  # tests/example/acme/unsafe.py: a test names it
_.schema_on_the_decorator  # tests/example/acme/graph.py: a test names it
_.schema_on_the_class  # tests/example/acme/graph.py: a test names it
_.runtime_in_the_schema  # tests/example/acme/graph.py: a test names it
_.dict_schema  # tests/example/acme/graph.py: a test names it
_.model_config  # tests/example/acme/graph.py: pydantic reads it
_.args_schema  # tests/example/acme/graph.py: LangChain reads it
_.runtime_beside_the_schema  # tests/example/acme/graph.py: a test names it
_.single_input_tool  # tests/example/acme/graph.py: a test names it
_.answer_with_its_error  # tests/example/acme/web.py: named by import path in a test
_.answer_with_a_bad_header  # tests/example/acme/web.py: named by import path in a test
_.answer_through_a_strict_tool  # tests/example/acme/web.py: named by import path in a test
_.answer_refund  # tests/example/acme/web.py: named by import path in a test's scrimmage.toml
_.answer_report  # tests/example/acme/web.py: named by import path in a test
_.answer_two_paths  # tests/example/acme/web.py: named by import path in a test
_.protocol_version  # tests/test_web_requests.py: http.server reads it from the handler class
_.do_HEAD  # tests/test_web_requests.py: http.server calls do_<METHOD> by name
_.do_PUT  # tests/test_web_requests.py: http.server calls do_<METHOD> by name
_.title  # tests/fakes/docs_server.py: the MCP server calls its tools by name
_.answer_twice  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_through_a_tool  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_not_listed  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_with_the_runs_environment  # tests/example/acme/docs_bot.py: named in scrimmage.toml
_.answer_swallows_refusal  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_keeps_a_secret  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_two_servers  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_web_after_mcp  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_web_in_a_hook  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_with_a_token  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_from_a_callback  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.async_auth_flow  # tests/example/acme/docs_bot.py: httpx2 calls it on an Auth object
_.chatty  # tests/fakes/docs_server.py: the MCP server calls its tools by name
_.answer_newer_protocol  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_sse  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_fastmcp  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.answer_model  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.delete_doc  # tests/fakes/docs_server.py: the MCP server calls its tools by name
_.output_model  # tests/fakes/docs_server.py: read by the MCP server's result check
_._handle_resumption_request  # tests/test_mcp.py: a stand-in the MCP gate wraps by name
_.handle_get_stream  # tests/test_mcp.py: a stand-in the MCP gate wraps by name
_.signals_kept  # tests/conftest.py: a fixture, which pytest.mark.usefixtures names as text
_.fast_tests_start_no_process  # tests/conftest.py: an autouse fixture, which pytest runs by itself
_.no_sender_thread_outlives_its_test  # tests/conftest.py: an autouse fixture, run by pytest
_.every_record_line_matches_the_public_schema  # tests/conftest.py: autouse
_.collected_between_tests  # tests/test_service_processes.py: an autouse fixture, run by pytest
_.gated_sends_are_put_back  # tests/conftest.py: an autouse fixture, run by pytest
_.pytestmark  # tests/test_cli_json.py and test_check.py: pytest reads it from the module
_.nothing_pending  # tests/test_cli_in_process.py and test_results.py: an autouse fixture
_.tz  # tests/test_results.py: the argument datetime.now is called with, which a stand-in ignores
_.answer_with_a_deadline  # tests/example/acme/docs_bot.py: a test's scrimmage.toml names it
_.close_connection  # tests/test_web_requests.py: http.server reads it to end a connection
_.answer_cutting  # tests/example/acme/agent.py: a test's scrimmage.toml names it
_.refund_when_stock_fails  # tests/example/acme/agent.py: a test's scrimmage.toml names it
_.check_answer  # tests/example/acme/late.py: the agent looks tools up by name
_.answer_question_only  # tests/example/acme/guarded.py: a test's scrimmage.toml names it
_.answer_stripping  # tests/example/acme/guarded.py: a test's scrimmage.toml names it
_.answer_raising  # tests/example/acme/guarded.py: a test's scrimmage.toml names it
_.answer_raising_uncaught  # tests/example/acme/guarded.py: a test's scrimmage.toml names it
_.answer_registry  # tests/example/acme/guarded.py: a test's scrimmage.toml names it
_.answer_moderated  # tests/example/acme/guarded.py: a test's scrimmage.toml names it
_.answer_in_threads  # tests/example/acme/guarded.py: a test's scrimmage.toml names it
_.__call__  # scrimmage/asgi_gate.py: the server calls it, as an ASGI app
_.chat_sync  # tests/example/acme/service.py: a FastAPI route, which FastAPI calls
_.chat_held  # tests/example/acme/service.py: a FastAPI route, which FastAPI calls
_.left_running  # tests/example/acme/service.py: a FastAPI route, which FastAPI calls
_.stream_json  # tests/example/acme/service.py: a FastAPI route, which FastAPI calls
_.go_on  # tests/example/acme/service.py: a FastAPI route, which FastAPI calls
_._state  # scrimmage/process_gate.py: multiprocessing's Pool.__del__ reads it
_.dies  # tests/example/acme/service.py: a FastAPI route, which FastAPI calls
_._on_queue_feeder_error  # tests/test_service_processes.py: a queue's hook its feeder calls
_.kind_load  # tests/k8s/images.py: the kind tests call it once they load the runner image
_.LateStock  # tests/test_tool_gate.py: named only in a string annotation, on purpose
_.CallbackManagerForToolRun  # tests/test_tool_gate.py: named only in a string annotation
_.https_open  # scrimmage/prices_update.py: urllib calls an HTTPS handler's https_open by name
_.do_DELETE  # tests/test_ungated.py: http.server calls do_<METHOD> by name
_.total_spend  # tests/test_docs_examples_real.py: pytest runs an autouse fixture by itself
