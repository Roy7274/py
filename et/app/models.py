from dataclasses import dataclass
from datetime import datetime
from enum import Enum

class TranscationType(Enum):
    """交易类型枚举"""
    INCOME = "收入"
    EXPENSE = "支出"

@dataclass
class Transaction:
    id: int
    type: TranscationType
    amount: float
    category: str
    note: str
    date: str

    @classmethod
    def create(cls, id: int, type_str: str, amount: float, category: str, note: str):
        
        return cls(
            id=id,
            type=TranscationType(type_str),
            amount=amount,
            category=category,
            note=note,
            date=datetime.now().strftime("%Y-%m-%d")
        )
    
    def to_dict(self) -> dict:
        return {
            "id":self.id,
            "type":self.type.value,
            "amount":self.amount,
            "category":self.category,
            "note": self.note,
            "date": self.date
        }
    @classmethod
    def from_dict(cls,data: dict) -> "Transaction" :
        return cls(
            id=data["id"],
            type=TranscationType(data["type"]),
            amount=data["amount"],
            category=data["category"],
            note= data["note"],
            date= data["date"]
        )
    class Config:
        from_attributes = True
if __name__ == "__main__":
    t1 = Transaction.create(
        id=1, 
        type_str="收入", 
        amount=10000.0, 
        category="工资", 
        note="3月工资"
    )
    
    t2 = Transaction.create(
        id=2, 
        type_str="支出", 
        amount=35.5, 
        category="餐饮", 
        note="午餐"
    )
    
    # 打印（@dataclass 自动生成了好用的 __repr__）
    print(t1)
    print(t2)
    
    # 测试序列化与反序列化
    d = t1.to_dict()
    print(f"\n序列化为字典: {d}")
    
    t3 = Transaction.from_dict(d)
    print(f"反序列化回来: {t3}")