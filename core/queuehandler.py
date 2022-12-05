import time
import asyncio
import discord
import traceback
import threading
import requests

from core import utility
from core import settings


# any command that needs to wait on processing should use the dream thread
class DreamQueueInstance:
    def __init__(self, web_ui: utility.WebUI):
        self.web_ui = web_ui

        self.dream_thread = threading.Thread()
        self.queue_inprogress: list[utility.DreamObject] = []
        self.queue: list[utility.DreamObject] = []

        self.last_data_model: str = None

    def process_dream(self, queue_object: utility.DreamObject):
        # append dream to queue
        self.queue.append(queue_object)

        # start dream queue thread
        if self.dream_thread.is_alive() == False:
            self.dream_thread = threading.Thread(target=self.process_queue, daemon=True)
            self.dream_thread.start()

    def process_queue(self):
        active_thread = threading.Thread()
        buffer_thread = threading.Thread()

        while self.queue:
            queue_object = self.queue.pop(0)
            try:
                # start next dream (older non-buffered queue)
                # queue_object.cog.dream(queue_object)

                # append queue object to in progress list
                self.queue_inprogress.append(queue_object)

                # queue up dream while the active thread is still running
                if active_thread.is_alive() and buffer_thread.is_alive():
                    active_thread.join()
                    active_thread = buffer_thread
                    buffer_thread = threading.Thread()
                if active_thread.is_alive():
                    buffer_thread = active_thread

                active_thread_event = threading.Event()
                active_thread = threading.Thread(target=queue_object.cog.dream, args=[queue_object, self.web_ui, active_thread_event], daemon=True)
                active_thread.start()

                if type(queue_object) is utility.DrawObject:
                    self.last_data_model = queue_object.data_model

                # wait for active thread to complete, or event to activate (indicating it is safe to continue)
                def wait_for_join():
                    active_thread.join()
                    active_thread_event.set()
                    try:
                        self.queue_inprogress.pop(0) # remove in progress object after completion
                    except:
                        pass
                wait_thread_join = threading.Thread(target=wait_for_join, daemon=True)
                wait_thread_join.start()
                active_thread_event.wait()

            except Exception as e:
                print(f'Dream failure:\n{queue_object}\n{e}\n{traceback.print_exc()}')
                # reset inprogress list in case of failure
                if self.queue_inprogress: self.queue_inprogress = []

    def clear_user_queue(self, user_id: int):
        total_cleared: int = 0
        index = len(self.queue)
        while index > 0:
            index -= 1
            user_compare = utility.get_user(self.queue[index].ctx)
            if user_id == user_compare.id:
                self.queue.pop()
                total_cleared += 1
        return total_cleared

    def get_user_queue_length(self, user_id):
        queue_length = 0
        queue = self.queue + self.queue_inprogress
        for dream_object in queue:
            user_compare = utility.get_user(dream_object.ctx)
            if user_id == user_compare.id:
                queue_length += 1
        return queue_length

    def get_queue_length(self):
        return len(self.queue_inprogress) + len(self.queue)

    def is_valid(self, queue_object: utility.DreamObject):
        # check if webui is online
        if self.web_ui.online == False:
            return False

        # check if models exist in this Web UI instance
        if type(queue_object) is utility.DrawObject:
            if queue_object.data_model not in self.web_ui.data_models:
                return False
            if queue_object.style != None and queue_object.style != 'None' and queue_object.style not in self.web_ui.style_names:
                return False

        if type(queue_object) is utility.UpscaleObject:
            if queue_object.upscaler_1 and queue_object.upscaler_1 not in self.web_ui.upscaler_names:
                return False
            if queue_object.upscaler_2 and queue_object.upscaler_2 not in self.web_ui.upscaler_names:
                return False

        return True

    def is_ready(self, buffer_limit: int = 2):
        # check if too many items are queued up
        if self.get_queue_length() >= buffer_limit:
            return False

        # check if webui is online
        # if not self.is_valid(queue_object):
        #     return False

        return True

# queue handler for dreams
class DreamQueue:
    def __init__(self):
        self.dream_instances: list[DreamQueueInstance] = []

        self.dream_thread = threading.Thread()
        self.queue_high: list[utility.DreamObject] = []
        self.queue_medium: list[utility.DreamObject] = []
        self.queue_low: list[utility.DreamObject] = []
        self.queue_lowest: list[utility.DreamObject] = []

        self.queues: list[list[utility.DreamObject]] = [self.queue_high, self.queue_medium, self.queue_low, self.queue_lowest]

    def setup(self):
        self.dream_instances = []
        for web_ui in settings.global_var.web_ui:
            self.dream_instances.append(DreamQueueInstance(web_ui))

    def process_dream(self, queue_object: utility.DreamObject, priority: str | int = 1, extended = True):
        if type(priority) is str:
            match priority:
                case 'high': priority = 0
                case 'medium': priority = 1
                case 'low': priority = 2
                case 'lowest': priority = 3

        if extended:
            valid_instances = self.get_valid_instances(queue_object)
            if len(valid_instances) == 0:
                print(f'Dream Rejected: No valid instances.')
                return None

            # get queue length
            queue_length = self.get_queue_length(priority)

            match priority:
                case 0: priority_string = 'High'
                case 1: priority_string = 'Medium'
                case 2: priority_string = 'Low'
                case 3: priority_string = 'Lowest'
            print(f'Dream Priority: {priority_string} - Queue: {queue_length}')

        # append dream to queue
        if type(priority) is int:
            self.queues[priority].append(queue_object)

        # start dream queue thread
        if self.dream_thread.is_alive() == False:
            self.dream_thread = threading.Thread(target=self.process_queue, daemon=True)
            self.dream_thread.start()

        if extended:
            return queue_length

    def process_queue(self):
        priority_index = 0
        queue_index = 0

        while priority_index < len(self.queues) or queue_index != 0:
            queue = self.queues[priority_index]
            if queue:
                if queue_index >= len(queue):
                    time.sleep(0.1)
                    queue_index = 0
                queue_object = queue[queue_index]
                try:
                    # Pick appropiate dream instance
                    target_dream_instance: DreamQueueInstance = None

                    # check if any instance is valid for queue
                    valid_instances: list[DreamQueueInstance] = self.get_valid_instances(queue_object)
                    if len(valid_instances) == 0:
                        # no available instance - remove the object from queue
                        queue.pop(queue_index)
                        queue_index = 0
                        user = utility.get_user(queue_object.ctx)
                        content = f'<@{user.id}> Sorry, I am currently offline.'
                        upload_queue.process_upload(utility.UploadObject(queue_object=queue_object, content=content, ephemeral=True, delete_after=30))

                    else:
                        # start dream on any available webui instance
                        for dream_instance in valid_instances:
                            if dream_instance.is_ready():
                                target_dream_instance = dream_instance
                                break

                        if target_dream_instance == None:
                            # no instance is suitable, try next item in line
                            queue_index += 1
                        else:
                            # start the dream in the instance
                            queue.pop(queue_index)
                            queue_index = 0
                            target_dream_instance.process_dream(queue_object)

                except Exception as e:
                    print(f'Dream failure:\n{queue_object}\n{e}\n{traceback.print_exc()}')

                priority_index = 0
            else:
                priority_index += 1

    def clear_user_queue(self, user_id: int):
        total_cleared: int = 0

        # clear from global dream queue
        for queue in self.queues:
            index = len(queue)
            while index > 0:
                index -= 1
                user_compare = utility.get_user(queue[index].ctx)
                if user_id == user_compare.id:
                    queue.pop()
                    total_cleared += 1

        # clear from all dream queue instances
        for dream_instance in self.dream_instances:
            total_cleared += dream_instance.clear_user_queue(user_id)

        return total_cleared

    def get_valid_instances(self, queue_object: utility.DreamObject):
        valid_instances: list[DreamQueueInstance] = []
        for dream_instance in self.dream_instances:
            if dream_instance.is_valid(queue_object):
                valid_instances.append(dream_instance)
        return valid_instances

    def get_queue_length(self, priority: int):
        queue_index = 0
        queue_length = 0

        # get length of global dream queue
        while queue_index <= priority:
            queue_length += len(self.queues[queue_index])
            queue_index += 1

        # get length of all dream isntances
        for dream_instance in self.dream_instances:
            queue_length += dream_instance.get_queue_length()

        return queue_length

    def get_user_queue_cost(self, user_id: int):
        queue_cost = 0.0
        queue: list[utility.DreamObject] = []

        # collect queues from dream instances
        for dream_instance in self.dream_instances:
            queue += dream_instance.queue_inprogress + dream_instance.queue

        # collect queues from global dream queue
        queue += self.queue_high + self.queue_medium + self.queue_low + self.queue_lowest

        # calculate dream cost of all queues the user has
        for queue_object in queue:
            user = utility.get_user(queue_object.ctx)
            if user and user.id == user_id:
                queue_cost += self.get_dream_cost(queue_object)
        return queue_cost

    # get estimate of the compute cost of a dream
    def get_dream_cost(self, queue_object: utility.DreamObject):
        if type(queue_object) is utility.DrawObject:
            dream_compute_cost_add = 0.0
            dream_compute_cost = float(queue_object.steps) / 20.0
            if queue_object.sampler in settings.global_var.slow_samplers: dream_compute_cost *= 2.0
            if queue_object.highres_fix: dream_compute_cost_add = dream_compute_cost
            dream_compute_cost *= pow(max(1.0, float(queue_object.width * queue_object.height) / float(512 * 512)), 1.25)
            if queue_object.init_url: dream_compute_cost *= max(0.2, queue_object.strength)
            dream_compute_cost += dream_compute_cost_add
            dream_compute_cost = max(1.0, dream_compute_cost)

            # use actual batch size from payload
            batch = queue_object.batch
            try:
                batch = queue_object.payload['n_iter']
            except:
                pass

            dream_compute_cost *= float(batch)

        elif type(queue_object) is utility.UpscaleObject:
            dream_compute_cost = queue_object.resize

        elif type(queue_object) is utility.IdentifyObject:
            dream_compute_cost = 1.0
            if queue_object.model == 'combined': dream_compute_cost *= len(settings.global_var.identify_models)

        return dream_compute_cost

# queue handler for uploads
class UploadQueue:
    def __init__(self):
        self.is_uploading = False
        self.event_loop = asyncio.get_event_loop()
        self.queue: list[utility.UploadObject] = []

    # upload the image
    def process_upload(self, queue_object: utility.UploadObject):
        # append upload to queue
        self.queue.append(queue_object)

        # start upload queue thread
        if self.is_uploading == False:
            self.is_uploading = True
            asyncio.run_coroutine_threadsafe(self.process_upload_queue(), self.event_loop)

    async def process_upload_queue(self):
        index = 0
        while self.queue:
            if index >= len(self.queue):
                await asyncio.sleep(0.1)
                index = 0
                # print('reset index')
            upload_object = self.queue[index]

            try:
                dream_wait: utility.DreamObject = upload_object.queue_object.wait_for_dream
                if dream_wait and dream_wait.uploaded == False:
                    # skip this object for now
                    index += 1
                    continue

                # send message
                ctx = upload_object.queue_object.ctx
                if upload_object.ephemeral:
                    if type(ctx) is discord.ApplicationContext:
                        message = await ctx.send_response(
                            content=upload_object.content, embed=upload_object.embed, files=upload_object.files, view=upload_object.view, delete_after=upload_object.delete_after)
                    elif type(ctx) is discord.Interaction:
                        try:
                            message = await ctx.response.send_message(
                                content=upload_object.content, embed=upload_object.embed, ephemeral=upload_object.ephemeral, files=upload_object.files, view=upload_object.view, delete_after=upload_object.delete_after)
                        except discord.InteractionResponded:
                            view = upload_object.view
                            if view == None: view = discord.MISSING
                            message = await ctx.followup.send(
                                content=upload_object.content, embed=upload_object.embed, ephemeral=upload_object.ephemeral, files=upload_object.files, view=view, delete_after=upload_object.delete_after)
                    elif type(ctx) is discord.Message:
                        message = await ctx.reply(
                            content=upload_object.content, embed=upload_object.embed, ephemeral=upload_object.ephemeral, files=upload_object.files, view=upload_object.view, delete_after=upload_object.delete_after)
                    else:
                        message = await ctx.channel.send(
                            content=upload_object.content, embed=upload_object.embed, files=upload_object.files, view=upload_object.view, delete_after=upload_object.delete_after)
                else:
                    message = await ctx.channel.send(
                        content=upload_object.content, embed=upload_object.embed, files=upload_object.files, view=upload_object.view, delete_after=upload_object.delete_after)

                upload_object.queue_object.uploaded = True

                # cache command
                if type(upload_object.queue_object) is utility.DrawObject and upload_object.queue_object.write_to_cache:
                    settings.append_dream_command(message.id, upload_object.queue_object.get_command())

                self.queue.remove(upload_object)

            except (requests.exceptions.RequestException, asyncio.exceptions.TimeoutError) as e:
                # connection error, return items to queue
                print(f'Upload connection error, retrying:\n{e}\n{traceback.print_exc()}')
                await asyncio.sleep(5.0)

            except Exception as e:
                print(f'Upload failure:\n{e}\n{traceback.print_exc()}')
                upload_object.queue_object.uploaded = True
                try:
                    self.queue.remove(upload_object)
                except:
                    pass

            index = 0

        self.is_uploading = False

dream_queue = DreamQueue()
upload_queue = UploadQueue()

dream_queue.setup()
