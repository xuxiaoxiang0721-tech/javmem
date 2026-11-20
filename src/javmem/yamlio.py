"""读 YAML 的规矩，包头、邮件头和配置都用它。"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

import yaml


class StrictLoader(yaml.SafeLoader):
    """比 SafeLoader 严三处。

    时间不自动转成 datetime，原样留文本，由调用方校验时区。不然没带时区的时间也会变成 datetime。
    同一层写了两个一样的键就报错。不然后一个会悄悄盖掉前一个，比如写了两行 sensitive。
    写不进账本的文字就报错，见 _strict_text。
    """

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict:
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=True)
                if isinstance(key, Hashable):
                    if key in seen:
                        raise yaml.constructor.ConstructorError(
                            None, None, "重复的键", key_node.start_mark
                        )
                    seen.add(key)
        return super().construct_mapping(node, deep)


def _strict_text(loader: StrictLoader, node: yaml.ScalarNode) -> str:
    """YAML 能用转义写出孤立代理（"\\uD800"），它不是合法的文字，到写库时才会炸。

    文件本身是严格 UTF-8 的，所以 YAML 是这种字符唯一的来路，在这里拦住就够了。
    """
    text = loader.construct_scalar(node)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise yaml.constructor.ConstructorError(None, None, "有不合法的字符", node.start_mark) from None
    return text


StrictLoader.yaml_implicit_resolvers = {
    first_char: [item for item in resolvers if item[0] != "tag:yaml.org,2002:timestamp"]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
StrictLoader.add_constructor("tag:yaml.org,2002:str", _strict_text)


def load_yaml(text: str) -> Any:
    """解析 YAML。出错时抛 yaml.YAMLError，错误信息里带原文，不要写进日志和回执。"""
    return yaml.load(text, Loader=StrictLoader)  # noqa: S506 StrictLoader 继承 SafeLoader
