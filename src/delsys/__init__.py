from .trigno import CHANNEL_COUNT, SAMPLE_RATE, TrignoClient, TrignoError

__all__ = ["CHANNEL_COUNT", "SAMPLE_RATE", "TrignoClient", "TrignoError"]

def main() -> int:
    from trigno_emg import main as cli_main
    return cli_main()