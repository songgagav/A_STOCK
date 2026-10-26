import daemon


def test_p08_no_frame_is_explicitly_blocked_not_success():
    status = daemon._p08_close_result_status(
        1,
        '{"day":"20261026","status":"NO_FRAME","message":"no frame"}',
    )

    assert status == "NO_FRAME"


def test_p08_success_is_only_reported_for_zero_exit_ok_payload():
    assert daemon._p08_close_result_status(0, '{"day":"20261026","status":"OK"}') == "OK"
    assert daemon._p08_close_result_status(1, "unexpected failure") == "ERROR"
