import paddle
from paddle import version as paddle_version

version_installed = [
        paddle_version.major, paddle_version.minor, paddle_version.patch,
        paddle_version.rc
    ]

for i in version_installed:
    print(i)