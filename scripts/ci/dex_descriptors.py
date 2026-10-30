"""Validate JVM/Dalvik descriptors against complete Java method shapes."""
import re


def _dex_type_tokens(value: str, allow_void: bool) -> list[str]:
    tokens = []
    index = 0
    primitive = set('BCDFIJSZ') | ({'V'} if allow_void else set())
    while index < len(value):
        start = index
        while index < len(value) and value[index] == '[':
            index += 1
        if index >= len(value):
            raise ValueError('Method descriptor has an incomplete array type')
        code = value[index]
        if code == 'L':
            end = value.find(';', index)
            owner = value[index + 1:end] if end >= 0 else ''
            if (end < 0 or not owner or any(c in owner for c in '.[];')
                    or owner.startswith('/') or owner.endswith('/') or '//' in owner):
                raise ValueError('Method descriptor has an invalid object type')
            index = end + 1
        elif code in primitive:
            index += 1
            if code == 'V' and index - start != 1:
                raise ValueError('Void cannot be an array type')
        else:
            raise ValueError('Method descriptor has an invalid type')
        tokens.append(value[start:index])
    return tokens


def _java_type_descriptor(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError('Method shape has an empty type')
    dimensions = 0
    while value.endswith('[]'):
        dimensions += 1
        value = value[:-2]
    primitive = {'void': 'V', 'boolean': 'Z', 'byte': 'B', 'char': 'C', 'short': 'S',
                 'int': 'I', 'long': 'J', 'float': 'F', 'double': 'D'}
    code = primitive.get(value)
    if code is None:
        if not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$.]*', value):
            raise ValueError('Method shape has an invalid Java type')
        code = 'L' + value.replace('.', '/') + ';'
    if code == 'V' and dimensions:
        raise ValueError('Void cannot be an array type')
    return '[' * dimensions + code


def _validate_target(target_id: str, target: dict) -> None:
    if not isinstance(target, dict) or set(target) != {'descriptor', 'shape'}:
        raise ValueError(f'Expected target {target_id} has an incomplete descriptor')
    descriptor = target.get('descriptor')
    match = re.fullmatch(r'(L[^;]+;)->([^\s(;]+)\(([^)]*)\)(.+)', descriptor or '')
    if not match:
        raise ValueError(f'Expected target {target_id} has an invalid method descriptor')
    owner, method_name = match.group(1)[1:-1], match.group(2)
    if (owner.startswith('/') or owner.endswith('/') or '//' in owner
            or any(character in owner for character in '.[];')
            or any(character in method_name for character in '.[/;')):
        raise ValueError(f'Expected target {target_id} has an invalid method descriptor')
    params = _dex_type_tokens(match.group(3), allow_void=False)
    result = _dex_type_tokens(match.group(4), allow_void=True)
    if len(result) != 1:
        raise ValueError(f'Expected target {target_id} has an invalid return descriptor')
    shape = target.get('shape')
    if (not isinstance(shape, dict) or set(shape) != {'parameters', 'returnType', 'isStatic'}
            or not isinstance(shape.get('parameters'), list)
            or type(shape.get('isStatic')) is not bool):
        raise ValueError(f'Expected target {target_id} has an incomplete method shape')
    try:
        shape_params = [_java_type_descriptor(item) for item in shape['parameters']]
        shape_return = _java_type_descriptor(shape.get('returnType'))
    except (TypeError, ValueError) as error:
        raise ValueError(f'Expected target {target_id} has an invalid method shape') from error
    if params != shape_params or result[0] != shape_return:
        raise ValueError(f'Expected target {target_id} descriptor and shape do not match')
