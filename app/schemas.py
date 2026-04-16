from pydantic import BaseModel, Field

class TransactionCreate(BaseModel):
    """接收前端创建交易的请求体"""
    type: str = Field(..., description="交易类型：收入 或 支出")
    amount: float = Field(..., gt=0, description="金额，必须大于0")
    category: str = Field(..., min_length=1, description="分类")
    note: str = Field("", description="备注，默认为空")

class TransactionResponse(BaseModel):
    """返回给前端的响应格式"""
    id: int
    type: str
    amount: float
    category: str
    note: str
    date: str

    # Pydantic 的配置项，允许从 ORM/自定义对象读取数据
    class Config:
        from_attributes = True 