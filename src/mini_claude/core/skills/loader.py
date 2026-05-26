from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Skill:
    name: str
    description: str
    system_prompt_template: str
    allowed_tools: list[str] = field(default_factory=list)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# 解析 Markdown skill 文件，提取 frontmatter 和正文 system prompt
def _parse_skill_file(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    name = path.stem
    description = ""
    allowed_tools: list[str] = []
    body = text

    m = _FRONTMATTER_RE.match(text)
    if m:
        front = m.group(1)
        body = text[m.end():]
        for line in front.splitlines():
            line = line.strip()
            if line.startswith("description:"):
                description = line[len("description:"):].strip().strip('"').strip("'")
            elif line.startswith("name:"):
                name = line[len("name:"):].strip().strip('"').strip("'")
            elif line.startswith("- ") and allowed_tools is not None:
                allowed_tools.append(line[2:].strip())
            elif line.startswith("allowed_tools:"):
                # 行内列表形式 allowed_tools: [a, b] — 暂不支持，只支持缩进列表
                pass

    return Skill(
        name=name,
        description=description,
        system_prompt_template=body.strip(),
        allowed_tools=allowed_tools,
    )


# 按三级优先级（项目本地 > 用户全局 > 内建）查找并解析 skill
class SkillLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # 按优先级查找 skill 文件；未找到返回 None
    def resolve(self, name: str) -> Skill | None:
        for path in self._search_paths(name):
            if path.exists():
                try:
                    return _parse_skill_file(path)
                except Exception:
                    return None
        return None

    # 返回 [项目本地, 用户全局, 内建] 路径列表；resolve() 返回第一个存在的，项目本地优先级最高
    def _search_paths(self, name: str) -> list[Path]:
        builtin = self._BUILTIN_DIR / f"{name}.md"
        global_ = Path("~/.mini/skills").expanduser() / f"{name}.md"
        local = Path(".mini/skills") / f"{name}.md"
        return [local, global_, builtin]

    # 列出所有可用 skill 名称（内建 + 用户全局 + 项目本地，去重后以项目本地覆盖为准）
    def list_all(self) -> list[str]:
        seen: dict[str, None] = {}
        for d in [
            self._BUILTIN_DIR,
            Path("~/.mini/skills").expanduser(),
            Path(".mini/skills"),
        ]:
            if d.exists():
                for f in sorted(d.glob("*.md")):
                    seen[f.stem] = None
        return list(seen)

    # 列出所有可用 Skill 对象（含描述），项目本地覆盖同名内建
    def list_all_skills(self) -> list[Skill]:
        seen: dict[str, Skill] = {}
        for d in [
            self._BUILTIN_DIR,
            Path("~/.mini/skills").expanduser(),
            Path(".mini/skills"),
        ]:
            if d.exists():
                for f in sorted(d.glob("*.md")):
                    try:
                        skill = _parse_skill_file(f)
                        seen[skill.name] = skill
                    except Exception:
                        pass
        return list(seen.values())

    # 将 $ARGUMENTS 替换为用户传入的参数字符串
    def render_prompt(self, skill: Skill, arguments: str) -> str:
        return skill.system_prompt_template.replace("$ARGUMENTS", arguments)
