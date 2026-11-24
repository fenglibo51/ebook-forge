import os

from .convert import ConvertError, convert, read_comic

__all__ = ["convert", "read_comic", "ConvertError"]

# 版本号只有这一处。
#
# 取值顺序：
#   ① EBOOKFORGE_VERSION —— Dockerfile 里 ARG VERSION 转成的环境变量，
#      CI 打 tag 时传进来。镜像里显示的就是 tag 本身，不会和镜像标签对不上。
#   ② 源码兜底值 —— 直接从源码跑（python -m bookforge）时用，和 pyproject 一致。
#
# 本地 docker build 不传 build-arg → 得到 "dev"，永远不会冒充正式版。
__version__ = os.environ.get("EBOOKFORGE_VERSION") or "1.1.4"
