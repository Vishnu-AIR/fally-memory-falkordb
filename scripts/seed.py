"""python -m scripts.seed : (re)load every synthetic user into FalkorDB."""

from synth.load import load_all

if __name__ == "__main__":
    for row in load_all():
        print(f"seeded {row['user']}: {row['blocks']} sources")
