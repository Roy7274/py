from app.models import Transaction, TranscationType
from app.storage import load_transactions,save_transactions

class ExpenseService:
    def __init__(self):
        self.transactions: list[Transaction] = load_transactions()
    
    def add_transaction(self, type_str: str, amount: float,
                        category: str, note: str) -> Transaction:
        if self.transactions:
            new_id = max(t.id for t in self.transactions) + 1
        else:
            new_id = 1
        t = Transaction.create(
            id = new_id,
            type_str=type_str,
            amount=amount,
            category=category,
            note=note
        )
        self.transactions.append(t)
        save_transactions(self.transactions)
        return t
    def delete_transaction(self, id: int) -> bool:
        length_before = len(self.transactions)

        self.transactions = [t for t in self.transactions if t.id != id]

        deleted = len(self.transactions) < length_before

        if deleted:
            save_transactions(self.transactions)
        return deleted
    def list_all(self) -> list[Transaction]:
        return self.transactions
    
    def find_by_id(self,id: int) -> Transaction | None:
        result = [t for t in self.transactions if t.id == id]
        return result[0] if result else None
    
    def find_by_category(self,category:str) -> list[Transaction]:
        result = [t for t in self.transactions if t.category == category]
        return result
    
    def find_by_month(self,year:int,month:int) ->list[Transaction]:
        prefix =f"{year}-{month:02d}"
        return [t for t in self.transactions if t.date.startswith(prefix)]
    
    def get_balance(self) -> float:
        total_income = sum(t.amount for t in self.transactions
                           if t.type == TranscationType.INCOME)
        total_expense = sum(t.amount for t in self.transactions
                            if t.type == TranscationType.EXPENSE)
        
        return round(total_income - total_expense, 2)
    
    def get_total_income(self) -> float:
        return round(sum(t.amount for t in self.transactions
                         if t.type == TranscationType.INCOME),2)
    
    def get_total_expense(self) -> float:
        return round(sum(t.amount for t in self.transactions
                         if t.type == TranscationType.EXPENSE),2)
    
    def get_category_summary(self) ->dict[str, float]:
        summary:dict[str,float] = {}
        for t in self.transactions:
            if t.type == TranscationType.EXPENSE:
                summary[t.category] = summary.get(t.category, 0) + t.amount
        sorted_summary = dict(
            sorted(summary.items(), key=lambda x:x[1],reverse=True)
        )
        return sorted_summary
    
    def get_month_summary(self, year: int, month: int) -> dict:
            """获取某月的统计摘要"""
            month_transactions = self.find_by_month(year, month)
            income = round(sum(t.amount for t in month_transactions
                            if t.type == TranscationType.INCOME), 2)
            expense = round(sum(t.amount for t in month_transactions
                            if t.type == TranscationType.EXPENSE), 2)
            return {
                "month": f"{year}-{month:02d}",
                "income": income,
                "expense": expense,
                "balance": round(income - expense, 2),
                "count": len(month_transactions)
            }


# ===== 测试代码 =====
if __name__ == "__main__":
    service = ExpenseService()

    # 清空旧数据重新测试
    service.transactions = []
    save_transactions(service.transactions)

    # 添加几笔交易
    service.add_transaction("收入", 15000.0, "工资", "3月工资")
    service.add_transaction("收入", 500.0, "兼职", "代课")
    service.add_transaction("支出", 35.5, "餐饮", "午餐")
    service.add_transaction("支出", 88.0, "餐饮", "晚餐")
    service.add_transaction("支出", 200.0, "交通", "打车")
    service.add_transaction("支出", 599.0, "购物", "耳机")
    service.add_transaction("支出", 1500.0, "购物", "衣服")

    print("=" * 50)
    print("📋 所有交易记录：")
    print("=" * 50)
    for t in service.list_all():
        symbol = "📥" if t.type == TranscationType.INCOME else "📤"
        print(f"  {symbol} #{t.id}  {t.date}  {t.category:<6}  ¥{t.amount:>8.1f}  {t.note}")

    print(f"\n💰 总收入: ¥{service.get_total_income()}")
    print(f"💸 总支出: ¥{service.get_total_expense()}")
    print(f"📊 结余:   ¥{service.get_balance()}")

    print(f"\n🏷️ 支出分类统计：")
    for category, amount in service.get_category_summary().items():
        bar = "█" * int(amount / 50)
        print(f"  {category:<6} ¥{amount:>7.1f}  {bar}")

    print(f"\n📅 本月摘要：")
    summary = service.get_month_summary(2026, 4)
    print(f"  月份: {summary['month']}")
    print(f"  收入: ¥{summary['income']}")
    print(f"  支出: ¥{summary['expense']}")
    print(f"  结余: ¥{summary['balance']}")
    print(f"  笔数: {summary['count']}")