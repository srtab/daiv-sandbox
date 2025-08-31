from daiv_sandbox.schemas import ImageAttrs


def test_image_when_user_and_working_dir_are_empty():
    result = ImageAttrs.from_inspection({"Config": {"User": "", "WorkingDir": ""}})
    assert result.user == ""
    assert result.working_dir == "/archives"


def test_image_user_from_config_when_not_root():
    result = ImageAttrs.from_inspection({"Config": {"User": "testuser", "WorkingDir": "working_dir"}})
    assert result.user == "testuser"
    assert result.working_dir == "working_dir"


def test_image_working_dir_when_root():
    result = ImageAttrs.from_inspection({"Config": {"User": "root", "WorkingDir": ""}})
    assert result.user == "root"
    assert result.working_dir == "/archives"


def test_image_working_dir_from_config_when_not_root():
    result = ImageAttrs.from_inspection({"Config": {"User": "testuser", "WorkingDir": "/app"}})
    assert result.user == "testuser"
    assert result.working_dir == "/app"
