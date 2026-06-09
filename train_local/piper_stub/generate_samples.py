"""
ダミー generate_samples モジュール（import 充足用）。

openWakeWord の train.py は起動時に無条件で
    sys.path.insert(0, os.path.abspath(config["piper_sample_generator_path"]))
    from generate_samples import generate_samples
を実行する（本来は piper-sample-generator を指す）。

本プロジェクトは正例を VOICEVOX(gen_samples.py)で自前供給し --generate_clips を
使わないため Piper 本体は不要。この stub を import 先に置くことで「起動時 import」だけ
満たす。--generate_clips を付けなければ generate_samples() は呼ばれないので問題ない。

もし合成正例(Piper)も使いたくなったら、本物を clone して config.yaml の
piper_sample_generator_path をそちらへ向けること:
    git clone https://github.com/rhasspy/piper-sample-generator
"""


def generate_samples(*args, **kwargs):
    raise RuntimeError(
        "generate_samples は stub です（--generate_clips は本構成では未対応）。"
        "正例は VOICEVOX(gen_samples.py)で用意し、--generate_clips を付けずに実行してください。"
    )
