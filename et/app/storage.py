import json
import os
from app.models import Transaction

DATA_DIR = "data"
FILE_PATH = os.path.join(DATA_DIR,"transactions.json")

def _ensure_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)

def load_transactions() -> list[Transaction]:
    if not os.path.exists(FILE_PATH):
        return []
    
    with open(FILE_PATH, "r",encoding="utf-8") as f:
        data_list = json.load(f)

    return [Transaction.from_dict(d) for d in data_list]    

def save_transactions(transactions: list[Transaction]):
    _ensure_dir()

    data_list = [t.to_dict() for t in transactions]

    with open(FILE_PATH,"w",encoding='utf-8') as f:
        json.dump(data_list,f,indent=2,ensure_ascii=False)

if __name__ == "__main__":
    # ===== 测试：写入再读出 =====

    # 1) 创建几笔交易
    t1 = Transaction.create(id=1, type_str="收入", amount=10000.0,
                            category="工资", note="3月工资")
    t2 = Transaction.create(id=2, type_str="支出", amount=35.5,
                            category="餐饮", note="午餐")
    t3 = Transaction.create(id=3, type_str="支出", amount=200.0,
                            category="交通", note="打车")

    transactions = [t1, t2, t3]

    # 2) 保存到文件
    save_transactions(transactions)
    print("✅ 已保存到", FILE_PATH)

    # 3) 从文件读回
    loaded = load_transactions()
    print(f"✅ 读回了 {len(loaded)} 条记录：")
    for t in loaded:
        print(f"   {t.date}  {t.type.value}  {t.category:　<6}  ¥{t.amount:>8.1f}  {t.note}")