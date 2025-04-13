from app import (
    send_welcome,
    handle_prediction,
    admin_panel,
    overall_system_check,
    request_live_prediction,
    admin_actions
)
import random


class FakeChat:
    def __init__(self, id):
        self.id = id

class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.username = f'testuser{user_id}'
        self.first_name = f'Test{user_id}'
        self.last_name = 'User'

class FakeMessage:
    def __init__(self, user_id, text):
        self.chat = FakeChat(user_id)
        self.from_user = FakeUser(user_id)
        self.text = text
        self.message_id = random.randint(1000, 9999)

class FakeCall:
    def __init__(self, user_id, data):
        self.message = FakeMessage(user_id, "dummy")
        self.from_user = FakeUser(user_id)
        self.data = data
        self.id = random.randint(10000, 99999)
        
        def run_load_test():
    print("Starting load test for 100 users...")
    
    # Track timing
    start_time = time.time()
    
    # Simulate 100 users
    for user_id in range(1, 101):
        print(f"Simulating user {user_id}...")
        
        # 1. Each user starts with /start command
        start_msg = FakeMessage(user_id, "/start")
        send_welcome(start_msg)
        
        # 2. 70% of users request a prediction
        if random.random() < 0.7:
            pred_call = FakeCall(user_id, "get_prediction")
            handle_prediction(pred_call)
        
        # 3. 20% of users request live prediction
        if random.random() < 0.2:
            live_call = FakeCall(user_id, "request_live")
            request_live_prediction(live_call)
        
        # 4. First user is treated as admin
        if user_id == 1:
            # Admin panel access
            admin_msg = FakeMessage(user_id, "/admin")
            admin_panel(admin_msg)
            
            # Admin checks system status
            status_call = FakeCall(user_id, "status_overall")
            overall_system_check(status_call)
            
            # Admin checks requests
            req_call = FakeCall(user_id, "check_requests")
            admin_actions(req_call)
    
    # Calculate and print results
    duration = time.time() - start_time
    print(f"\nLoad test completed for 100 users in {duration:.2f} seconds")
    print(f"Average response time: {duration/100:.3f} seconds per user")
    
    
    if __name__ == "__main__":
    run_load_test()