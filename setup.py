from setuptools import find_packages, setup

setup(
    name="servo-fuzzing",
    version="0.1.0",
    description="Servo Fuzzing Test Suite and Triage Harness",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "servo-fuzzing=servo_fuzzing.cli:main",
        ],
    },
    python_requires=">=3.9",
)
