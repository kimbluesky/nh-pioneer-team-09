"""종목 마스터 파일을 표로 바꿉니다.

형식은 단순합니다 — UTF-8 텍스트, 한 줄이 한 종목, 필드는 `;` 로 나뉩니다.
따옴표도 이스케이프도 없고, 종목명 안의 천 단위 구분자는 쉼표(`190,000`)라
필드를 쪼개지 않습니다. 그래서 `split(";")` 만으로 충분합니다.

파일이 아주 큽니다(아홉 개 합쳐 15만 행 안팎). 그래서 행을 사전이 아니라 **튜플**로
담고 컬럼 이름은 표에 한 번만 둡니다. 사전으로 담으면 컬럼 이름이 15만 번 반복되어
메모리가 몇 배로 늘어납니다. 이름으로 꺼내 쓰고 싶으면 `MasterTable.column` 이나
`MasterTable.rows_as_dicts()` 를 쓰세요.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

#: 마스터 파일의 인코딩. UTF-8 입니다.
#:
#: 자동으로 다른 인코딩을 넣어 보는 대비책은 **일부러 두지 않습니다.** 다른
#: 인코딩으로 읽으면 대개 예외 없이 엉뚱한 글자로 읽히기 때문에, 종목명이 깨진 채
#: 화면까지 흘러가고 아무도 모릅니다. 못 읽으면 그 파일을 실패로 두는 편이 낫습니다.
ENCODING = "utf-8"

#: 필드 구분자.
DELIMITER = ";"


class MasterParseError(Exception):
    """마스터 파일을 읽지 못한 경우입니다. 어느 줄에서 어긋났는지 문구에 담습니다."""


@dataclass(frozen=True)
class MasterTable:
    """파싱한 마스터 파일 하나.

    `fields` 가 컬럼 이름이고 `rows` 의 각 튜플이 그 순서대로 값을 담습니다.
    """

    #: 파일 이름에서 확장자를 뺀 것 (`fut_opt`).
    name: str
    #: 국내(`domestic`) 인지 해외(`overseas`) 인지.
    market: str
    #: 컬럼 이름. NH선물 헤더정보 문서의 표기 그대로입니다.
    fields: tuple[str, ...]
    #: 한 행이 한 종목입니다. 값은 전부 문자열이며 숫자로 바꾸지 않습니다.
    rows: tuple[tuple[str, ...], ...]

    def __len__(self) -> int:
        return len(self.rows)

    def index_of(self, field: str) -> int:
        """컬럼 이름의 자리 번호. 없는 이름이면 `KeyError` 입니다."""
        try:
            return self.fields.index(field)
        except ValueError as exc:
            raise KeyError(f"{self.name} 에 없는 컬럼입니다: {field!r}") from exc

    def column(self, field: str) -> tuple[str, ...]:
        """한 컬럼의 값을 행 순서대로 모아 돌려줍니다."""
        at = self.index_of(field)
        return tuple(row[at] for row in self.rows)

    def rows_as_dicts(self) -> Iterator[dict[str, str]]:
        """행을 `{컬럼 이름: 값}` 으로 하나씩 내놓습니다.

        한꺼번에 목록으로 만들면 메모리를 크게 쓰므로 필요한 만큼만 꺼내 쓰세요.
        """
        for row in self.rows:
            yield dict(zip(self.fields, row))


def parse_master_text(
    text: str,
    fields: Iterable[str],
    *,
    name: str = "",
    market: str = "",
) -> MasterTable:
    """마스터 파일의 내용을 표로 바꿉니다.

    Args:
        text: 파일 전체 내용.
        fields: 컬럼 이름. 파일 안 순서와 같아야 합니다.
        name: 어느 파일인지. 오류 문구에 씁니다.
        market: 국내/해외 구분.

    Raises:
        MasterParseError: 어느 한 줄이라도 필드 개수가 맞지 않는 경우.
            한 줄만 어긋나도 배치가 통째로 밀렸을 수 있어 그 파일은 쓰지 않습니다.
    """
    fields = tuple(fields)
    rows: list[tuple[str, ...]] = []

    # `splitlines()` 는 유니코드 줄바꿈(U+2028 등)에서도 줄을 나눕니다. 종목명에
    # 그런 문자가 들어오면 한 종목이 두 줄로 쪼개지므로 개행 문자만 기준으로 나눕니다.
    for number, line in enumerate(text.split("\n"), start=1):
        line = line.rstrip("\r")
        if not line.strip():
            # 파일 끝의 개행 때문에 마지막에 빈 줄이 남습니다. 빈 줄은 종목이 아닙니다.
            continue

        values = line.split(DELIMITER)
        if len(values) != len(fields):
            raise MasterParseError(
                f"{name or '마스터 파일'} {number}번째 줄의 항목 수가 맞지 않습니다"
                f" (기대 {len(fields)}개, 실제 {len(values)}개)"
            )

        # 값 앞뒤의 공백을 떼어 냅니다. 대부분의 행은 공백이 없지만 종목코드 뒤에
        # 공백이 붙어 오는 행이 실제로 있고, 그대로 두면 같은 종목을 코드로 찾지
        # 못합니다. 종목명 가운데의 정렬용 공백은 원문 그대로 남습니다.
        rows.append(tuple(value.strip() for value in values))

    return MasterTable(name=name, market=market, fields=fields, rows=tuple(rows))


def parse_master_file(
    path: Path,
    fields: Iterable[str],
    *,
    name: str = "",
    market: str = "",
) -> MasterTable:
    """마스터 파일을 읽어 표로 바꿉니다.

    Raises:
        MasterParseError: 파일을 못 읽거나, 인코딩이 UTF-8 이 아니거나,
            필드 개수가 맞지 않는 경우.
    """
    try:
        text = path.read_text(encoding=ENCODING)
    except UnicodeDecodeError as exc:
        raise MasterParseError(
            f"{name or path.name} 을 {ENCODING} 로 읽지 못했습니다. 파일이 손상되었거나"
            " 다른 인코딩으로 배포되었을 수 있습니다."
        ) from exc
    except OSError as exc:
        raise MasterParseError(f"{name or path.name} 을 열지 못했습니다: {exc}") from exc

    return parse_master_text(text, fields, name=name or path.stem, market=market)
