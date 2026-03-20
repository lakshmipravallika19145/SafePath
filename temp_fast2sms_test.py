import requests

url='https://www.fast2sms.com/dev/bulkV2'
headers={'authorization':'EUa8staznIqxRxDYi7k1ZhK8FiRaLUbdShtv7SZJhGvNQwFoLy6e4qnvZpHa'}
payload={'route':'v3','sender_id':'TXTIND','message':'test','language':'english','flash':0,'numbers':'9876543210'}

for ct in ['application/x-www-form-urlencoded','application/json',None]:
    headers2=headers.copy()
    if ct:
        headers2['Content-Type']=ct
    if ct=='application/json':
        r=requests.post(url,json=payload,headers=headers2,timeout=30)
    else:
        r=requests.post(url,data=payload,headers=headers2,timeout=30)
    print('CT',ct,'status',r.status_code,'text',r.text)
