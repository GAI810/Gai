"""平台抽象基类"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Candidate:
    """候选人数据"""
    platform: str
    platform_id: str
    name: str
    title: str = ""
    company: str = ""
    experience: str = ""
    profile_url: str = ""
    search_keyword: str = ""
    education: str = ""
    skills: list = field(default_factory=list)
    detail: str = ""


@dataclass
class SearchFilters:
    """搜索筛选条件"""
    city: str = ""
    experience: str = ""      # 如 "3-5年"
    education: str = ""       # 如 "本科"
    salary: str = ""          # 如 "15-25K"
    industry: str = ""
    extra: dict = field(default_factory=dict)


class BasePlatform(ABC):
    """所有招聘平台的公共接口"""

    @property
    @abstractmethod
    def name(self) -> str:
        """平台标识名，如 'boss', 'maimai'"""
        ...

    @abstractmethod
    def is_logged_in(self, page) -> bool:
        """检测当前页面是否已登录"""
        ...

    @abstractmethod
    def search_candidates(self, page, keywords: list[str],
                          filters: Optional[SearchFilters] = None,
                          max_results: int = 100) -> list[Candidate]:
        """搜索候选人列表"""
        ...

    @abstractmethod
    def send_greeting(self, page, candidate: Candidate) -> bool:
        """向候选人发送打招呼消息，返回是否成功"""
        ...

    @abstractmethod
    def check_replies(self, page) -> list[Candidate]:
        """检查会话列表，返回已回复的候选人"""
        ...

    @abstractmethod
    def request_resume(self, page, candidate: Candidate) -> bool:
        """请求/获取候选人简历，返回是否成功"""
        ...

    @abstractmethod
    def download_resume(self, page, candidate: Candidate, save_dir: str) -> Optional[str]:
        """下载候选人简历到本地，返回文件路径"""
        ...
