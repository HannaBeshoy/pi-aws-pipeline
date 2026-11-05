"""Shared test helpers: load the CloudFormation template and resolve the bits the tests need."""
import os, re, sys
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "device"), os.path.join(ROOT, "tools")]


class CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        v = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        v = loader.construct_sequence(node, deep=True)
    else:
        v = loader.construct_mapping(node, deep=True)
    name = "Ref" if suffix == "Ref" else "Fn::" + suffix
    return {name: v}


CfnLoader.add_multi_constructor("!", _tag)


def template():
    with open(os.path.join(ROOT, "infra", "template.yaml")) as f:
        return yaml.load(f, Loader=CfnLoader)


def resolve(x, values):
    """Resolve Ref, Fn::Sub and Fn::GetAtt with a dict of names -> values (enough for the policies)."""
    if isinstance(x, dict) and len(x) == 1:
        (k, v), = x.items()
        if k == "Ref":
            return values[v]
        if k == "Fn::GetAtt":
            return values[v if isinstance(v, str) else ".".join(v)]
        if k == "Fn::Sub":
            return re.sub(r"\$\{([^}]+)\}", lambda m: str(values[m.group(1)]), v)
    if isinstance(x, dict):
        return {k: resolve(v, values) for k, v in x.items()}
    if isinstance(x, list):
        return [resolve(v, values) for v in x]
    return x


def load_module(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
