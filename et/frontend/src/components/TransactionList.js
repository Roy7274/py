
function TransactionList({transactions}) {
    if (transactions.length === 0) {
        return <p>无记录</p>
    }

    return(
        <div>
            <h2>交易记录 ({transactions.length}条)</h2>
            <ul>
                {transactions.map(t => (
                    <li key={t.id}>
                        {t.type === '收入' ? '📥' : '📤'} 
                        {t.date} | 
                        {t.category} | 
                        ¥{t.amount} | 
                        {t.note}
                    </li>
                ))}
            </ul>
        </div>
    );
}
export default TransactionList;