import { useState } from "react";

function TransactionForm({onAdd}){
    const [type, setType] = useState('支出');
    const [amount, setAmount] = useState('');
    const [category, setCategory] = useState('');
    const [note, setNote] = useState('');

    const handleSubmit = (e) => {
        e.preventDefault();

        if(!amount || !category) return;

        onAdd({
            type: type,
            amount: amount,
            category: category,
            note: note
        });
        setAmount('');
        setCategory('');
        setNote('');
    };
    return(
        <div style={{marginTop: '20px', padding: '15px', border: '1px solid #ccc'}}>
            <h2>📝 添加记录</h2>
            <form onSubmit={handleSubmit}>
                <div>
                    <button type="button" onClick={() => setType('支出')}>
                        {type === '支出' ? '🔴' : '⚪'} 支出
                    </button>
                    <button type="button" onClick={() => setType('收入')} style={{ marginLeft: '10px' }}>
                        {type === '收入' ? '🟢' : '⚪'} 收入
                    </button>
                </div>

                <div style={{marginBottom: '10px'}}>
                金额：
                <input
                    type="number"
                    value={amount}
                    onChange={(e)=> setAmount(e.target.value)}
                    style={{marginLeft: '5px'}}
                    />
                </div>

                <div style={{ marginBottom: '10px' }}>
                分类：
                <input 
                    type="text" 
                    value={category} 
                    onChange={(e) => setCategory(e.target.value)} 
                    style={{ marginLeft: '5px' }}
                />
                </div>


                <div style={{ marginBottom: '10px' }}>
                备注：
                <input 
                    type="text" 
                    value={note} 
                    onChange={(e) => setNote(e.target.value)} 
                    style={{ marginLeft: '5px' }}
                />
                </div>

                <button type="submit">✅ 添加{type}</button>
            </form>
        </div>
    );
}

export default TransactionForm;