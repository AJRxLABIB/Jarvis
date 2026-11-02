from app.utils import gpu_pref


def test_integrated_hides_nvidia_and_sets_windows_preference():
    env, writes = {}, []
    done = gpu_pref.apply("integrated", environ=env, windows=True, registry=lambda exe, v: writes.append((exe, v)))
    assert env["CUDA_VISIBLE_DEVICES"] == "-1" and env["OLLAMA_VULKAN"] == "1"
    assert writes and all(v == "GpuPreference=1;" for _, v in writes) and any("Intel" in d for d in done)


def test_auto_undoes_only_what_we_set_and_clears_the_registry():
    env, writes = {}, []
    gpu_pref.apply("integrated", environ=env, windows=False)
    gpu_pref.apply("auto", environ=env, windows=True, registry=lambda e, v: writes.append(v))
    assert "CUDA_VISIBLE_DEVICES" not in env and set(writes) == {None}
    mine = {"CUDA_VISIBLE_DEVICES": "-1"}                       # the user's own setting is left alone
    gpu_pref.apply("auto", environ=mine, windows=False)
    assert mine["CUDA_VISIBLE_DEVICES"] == "-1"


def test_nvidia_and_registry_failure_never_raise():
    def boom(e, v): raise PermissionError
    done = gpu_pref.apply("nvidia", environ={}, windows=True, registry=boom)
    assert any("could not write" in d for d in done)
