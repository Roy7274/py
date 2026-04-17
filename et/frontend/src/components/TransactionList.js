
function TransactionList({transactions, onDelete}) {
    if (transactions.length === 0) {
        return (
            <div className="card">
                <h2>📋 交易记录</h2>
                <p className="empty">📭 暂无记录，快去记一笔吧！</p>
            </div>
      )
    }

    return(
<div className="card">
      <h2>📋 交易记录 ({transactions.length}条)</h2>
      <div className="transaction-list">
        {transactions.map(t => (
          <div key={t.id} className={`transaction-item ${t.type}`}>
            <div className="transaction-info">
              <span className="symbol">{t.type === '收入' ? '📥' : '📤'}</span>
              <div className="transaction-detail">
                <span className="category">{t.category}</span>
                <span className="note">{t.note}</span>
              </div>
              <span className="date">{t.date}</span>
            </div>
            <div className="transaction-right">
              <span className="amount">
                {t.type === '收入' ? '+' : '-'}¥{t.amount.toFixed(1)}
              </span>
              {/* 🆕 绑定删除事件 */}
              <button className="btn-delete" onClick={() => onDelete(t.id)}>
                🗑️
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
    );
}
export default TransactionList;