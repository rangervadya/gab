"""Validation shared by the import editor; no document recognition here."""
import re
from decimal import Decimal, InvalidOperation

EDIT_FIELDS = {
    'company_name', 'inn', 'city', 'full_name', 'position', 'phone', 'email',
    'revenue_rub', 'revenue_year', 'revenue_source', 'notes',
}


def validate_changes(changes):
    if not isinstance(changes, dict) or not changes or set(changes) - EDIT_FIELDS:
        raise ValueError('Передайте только разрешённые поля редактора')
    result = {}
    for key, value in changes.items():
        if value is not None and not isinstance(value, (str, int, float, Decimal)) or isinstance(value, bool):
            raise ValueError(f'{key}: недопустимый тип значения')
        value = str(value).strip() if value is not None else ''
        if len(value) > (4000 if key == 'notes' else 500):
            raise ValueError(f'{key}: слишком длинное значение')
        if not value:
            result[key] = None
            continue
        if key == 'email':
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
                raise ValueError('Некорректный email')
            value = value.lower()
        elif key == 'phone':
            if not re.fullmatch(r'\+?[\d\s().-]+', value) or not 7 <= len(re.sub(r'\D', '', value)) <= 15:
                raise ValueError('Телефон должен содержать от 7 до 15 цифр')
        elif key == 'inn':
            if not re.fullmatch(r'(?:\d{10}|\d{12})', value, flags=re.ASCII):
                raise ValueError('ИНН должен содержать 10 или 12 цифр')
            weights = [(2,4,10,3,5,9,4,6,8)] if len(value) == 10 else [(7,2,4,10,3,5,9,4,6,8), (3,7,2,4,10,3,5,9,4,6,8)]
            if len(set(value)) == 1 or any(sum(int(value[i])*w for i,w in enumerate(ws)) % 11 % 10 != int(value[len(ws)]) for ws in weights):
                raise ValueError('Неверная контрольная сумма ИНН')
        elif key == 'revenue_rub':
            try:
                value = Decimal(value.replace(' ', '').replace(',', '.'))
            except InvalidOperation:
                raise ValueError('Выручка должна быть числом') from None
            if not value.is_finite() or value < 0 or value >= Decimal('1000000000000000000') or value.as_tuple().exponent < -2:
                raise ValueError('Некорректная выручка: максимум 18 цифр и 2 знака после запятой')
        elif key == 'revenue_year':
            if not re.fullmatch(r'\d{4}', value) or not 1900 <= int(value) <= 2100:
                raise ValueError('Год должен быть от 1900 до 2100')
            value = int(value)
        result[key] = value
    return result
