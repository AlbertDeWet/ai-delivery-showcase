import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from deltarecover.demo import main  # noqa: E402

if __name__ == "__main__":
    import json
    r = main()
    print(json.dumps({"incident_a_after": r["incident_a"]["after"], "recovery": r["recovery"], "rollback": r["rollback_proof"]["status"]}, indent=2, default=str))
