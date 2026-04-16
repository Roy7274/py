from fastapi import APIRouter, HTTPException
from app.models import Transaction, TranscationType
from app.service import ExpenseService
from app.schemas import TransactionCreate,TransactionResponse

router = APIRouter(prefix="/api/transactions",tags=["交易管理"])

service = ExpenseService()

@router.get('/',response_model=list[TransactionResponse])
def get_all_transactions():
    return service.list_all()


@router.post("/", response_model=TransactionResponse, status_code=201)
def create_transaction(data: TransactionCreate):
    t = service.add_transaction(data.type, data.amount, data.category, data.note)
    return t

@router.delete("/{id}")
def delete_transaction(id: int):
    success = service.delete_transaction(id)
    if not success:
        raise HTTPException(status_code=404, detail=f"未找到 ID={id} 的记录")
    return {"message": "删除成功"}


# ===== 再加一个统计的 Router =====
stats_router = APIRouter(prefix="/api/statistics", tags=["统计报表"])

@stats_router.get("/")
def get_statistics():
    service = ExpenseService()
    return {
        "total_income": service.get_total_income(),
        "total_expense": service.get_total_expense(),
        "balance": service.get_balance(),
        "category_summary": service.get_category_summary()
    }