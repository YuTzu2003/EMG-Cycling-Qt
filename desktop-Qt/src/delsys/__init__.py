from .emg.trigno import CHANNEL_COUNT, SAMPLE_RATE, TrignoClient, TrignoError

__all__ = ["CHANNEL_COUNT", "SAMPLE_RATE", "TrignoClient", "TrignoError"]

def main() -> int:
    from .emg_cli import main as cli_main
    return cli_main()
