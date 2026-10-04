# A tour of the suite

Where each pytest technique lives, and why it's there. File and function names rather than line numbers, so this stays true as the code moves.

## Fixtures

**A factory fixture with checks on the way out.** `connect` in `tests/conftest.py` returns a function, not a client, so one test can open as many players as it needs (`a, b = connect(), connect()`). Its teardown closes every client, then makes two assertions about what happened during the test: every frame the server sent decoded under the strict decoder, and the local server logged no exception. So every test that plays the game is also a protocol-contract check and a server-health check, and a failure points at the test that caused it, not at whichever test ran next.

**Session scope per worker.** `target` is session-scoped. Under pytest-xdist each worker is its own session, so each worker starts its own seeded server. Workers never share a world, and a test that depletes every copper rock can't break a test on another worker.

**Building once, before the workers start.** The server is compiled in `pytest_sessionstart` on the controller only (`hasattr(config, "workerinput")` is false there). If each worker built it, four `dotnet build`s would fight over the same output folder.

**Small fixtures composed.** `player` is just `connect()`. `armed` in `test_combat.py` is a player who has been given Old Bram's kit. Tests ask for the state they need by name.

## Parametrize

**Tables of cases with readable ids.** `test_names_are_sanitised` in `test_session.py` runs five name inputs, each with an id (`markup`, `blank`, `long`...), so a failure says which case broke.

**Marking one case of many.** `test_gathering_shows_a_progress_status` uses `pytest.param(..., marks=pytest.mark.xfail(...))`, so only the Chop case is a known bug (BUG-003) and Pluck and Mine still have to pass.

**Parametrizing over data.** `test_gold_labels_use_the_classifiers_vocabulary` in `tests/unit/test_email_eval.py` makes one test per eval case, straight from the JSON.

## Markers

Registered in `pyproject.toml` with `--strict-markers`, so a typo in a marker name is an error, not a silently ignored mark. Every test gets a layer marker (module-level `pytestmark = [...]`) and an `area("...")` marker with an argument. `local_only` tests are skipped on the live target by `pytest_collection_modifyitems` in `conftest.py`, with the reason in the skip message.

## Known bugs as strict xfails

`xfail_strict = true` in `pyproject.toml`. A known bug is a test that asserts the *right* behaviour and is marked xfail with the bug id in the reason. While the bug exists, the test fails and the run stays green. When someone fixes it, the test passes, and strict mode turns that XPASS into a failure. Fixes can't go unnoticed, and the marker has to be removed on purpose. The report reads the `BUG-nnn` ids from the reasons to build the bug list.

## Waiting without sleeping

There's no `time.sleep` in a test. `GameClient.wait_until` in `qualitylab/spirescape/client.py` waits on a `threading.Condition` that the reader thread notifies on every message, so a wait ends the moment the state is right. When it times out it says what it was waiting for, the current objective and the last few server notices, which is usually enough to see what went wrong without rerunning.

Timeouts are counted in server ticks (`Budget.ticks` in `play.py`), so the same test waits under a second locally (20 ms tick) and up to a minute live (600 ms tick).

## Shared environments

On the live demo the world is shared with real players. `play.gather` and `play.start_fight` watch for the server's "someone got there first" notices, or for an action that started and was then cancelled, and retry with another rock or rat. That's not hiding flakiness: the retry only fires on a specific, expected race, and anything else still fails. The combat tests also share an `xdist_group("rats")` and run with `--dist loadgroup`, so they don't fight each other for the two rats.

## Hypothesis

**Strategies from the protocol itself.** `strategy_for` in `tests/unit/test_messages.py` turns any message layout into a Hypothesis strategy, so every message type, including ones added later, gets a generated round-trip test for free.

**Drawing inside the test.** `test_wherever_you_click_you_end_up_somewhere_walkable` uses `st.data()` to draw coordinates after it knows the map's size.

**Fuzzing a live process.** `test_random_bytes_never_hurt_the_server` and `test_well_formed_nonsense_is_handled` throw generated garbage, and valid frames with absurd values, at a real local server, then check a bystander can still play.

**Profiles.** `conftest.py` registers a default and a lighter `live` profile, and turns off the deadline, because network round trips aren't a fair thing to time per example.

## Hooks and a plugin

`qualitylab/results.py` is a pytest plugin. A `pytest_runtest_makereport` hookwrapper attaches each test's layer, area, docstring and location to its report, and a `Recorder` collects the reports in `pytest_runtest_logreport` and writes the run to JSON at `pytest_sessionfinish`. Under xdist only the controller writes, and it gets the labels because `user_properties` travel with reports from the workers. Tests report measurements with the built-in `record_property("metric", {...})`.

The plugin has its own tests in `tests/unit/test_results_plugin.py`, written with pytest's `pytester` fixture: it runs a tiny suite in a sandbox and checks the JSON that comes out.

## Browser tests

`tests/spirescape/test_ui.py` uses pytest-playwright's `page` fixture. Overriding `browser_type_launch_args` makes it use the installed Edge on Windows and the downloaded Chromium in CI. These found BUG-007, the tab title.

## Testing the AI parts

The model's output can't be unit tested, but the guardrails around it can. `tests/unit/test_analyst.py` checks that invented numbers and invented test names get struck, and that new suggestions never overwrite a decision. `tests/unit/test_email_eval.py` checks the scoring rules and the eval dataset itself: valid labels, evidence present in every body, and the long cases really past the 600-character cut.
