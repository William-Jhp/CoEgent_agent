// Local C ABI over the installed llama.cpp server-context. No sockets or child processes.
#include "server-context.h"
#include "arg.h"
#include "common.h"
#include "llama.h"
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <thread>

namespace {
char * copy_string(const std::string & value) {
    auto * result = static_cast<char *>(std::malloc(value.size() + 1));
    if (!result) { throw std::bad_alloc(); }
    std::memcpy(result, value.c_str(), value.size() + 1);
    return result;
}
struct engine {
    common_params params;
    server_context context;
    std::unique_ptr<server_routes> routes;
    std::thread loop;
    std::mutex request_mutex;
    ~engine() {
        context.terminate();
        if (loop.joinable()) { loop.join(); }
        routes.reset();
    }
};
std::once_flag initialized;
int fail(char ** output, const std::string & message, int code = 1) noexcept {
    try { *output = copy_string(message); } catch (...) { *output = nullptr; }
    return code;
}
}

extern "C" {
int coegent_llama_abi_version() noexcept { return 1; }
void coegent_llama_free(void * value) noexcept { std::free(value); }
int coegent_llama_open(const char * options, void ** handle, char ** error) noexcept {
    *handle = nullptr; *error = nullptr;
    try {
        auto config = json::parse(options);
        auto arguments = config.at("argv").get<std::vector<std::string>>();
        std::vector<char *> argv;
        for (auto & argument : arguments) { argv.push_back(argument.data()); }
        std::call_once(initialized, [] { common_init(); llama_backend_init(); });
        auto instance = std::make_unique<engine>();
        if (!common_params_parse(static_cast<int>(argv.size()), argv.data(), instance->params, LLAMA_EXAMPLE_SERVER)) {
            throw std::runtime_error("invalid llama.cpp options");
        }
        if (instance->params.n_parallel != 1 || instance->params.model.path.empty()) {
            throw std::runtime_error("inprocess requires one slot and a local model path");
        }
        instance->routes = std::make_unique<server_routes>(instance->params, instance->context);
        if (!instance->context.load_model(instance->params)) { throw std::runtime_error("model load failed"); }
        instance->routes->update_meta(instance->context);
        instance->loop = std::thread([ptr = instance.get()] { ptr->context.start_loop(); });
        *handle = instance.release();
        return 0;
    } catch (const std::exception & e) { return fail(error, e.what()); }
      catch (...) { return fail(error, "unknown native initialization error"); }
}

int coegent_llama_request(void * handle, const char * route, const char * body, double timeout_s,
                         int (*cancelled)(), char ** output) noexcept {
    *output = nullptr;
    try {
        if (!handle || !route || !body || !(timeout_s > 0)) { throw std::invalid_argument("invalid native request"); }
        auto & instance = *static_cast<engine *>(handle);
        std::lock_guard<std::mutex> lock(instance.request_mutex);
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::duration<double>(timeout_s);
        std::function<bool()> should_stop = [cancelled, deadline] {
            return (cancelled && cancelled()) || std::chrono::steady_clock::now() >= deadline;
        };
        if (should_stop()) { return fail(output, "request cancelled or timed out", 2); }
        auto data = json::parse(body);
        if (data.value("stream", false)) { throw std::invalid_argument("streaming is not supported by this binding"); }
        server_http_req request{{}, {}, route, "", body, {}, should_stop};
        auto & routes = *instance.routes;
        server_http_context::handler_t handler;
        const std::string path(route);
        if (path == "/v1/chat/completions") { handler = routes.post_chat_completions; }
        else if (path == "/props") { handler = routes.get_props; }
        else if (path == "/v1/models") { handler = routes.get_models; }
        else if (path == "/lora-adapters") { handler = routes.get_lora_adapters; }
        else if (path == "/apply-template") { handler = routes.post_apply_template; }
        else if (path == "/tokenize") { handler = routes.post_tokenize; }
        else if (path == "/completion") { handler = routes.post_completions; }
        else { throw std::invalid_argument("unsupported native route"); }
        auto response = handler(request);
        response->on_complete();
        if (should_stop()) { return fail(output, "request cancelled or timed out", 2); }
        *output = copy_string(json({{"status", response->status}, {"body", response->data}}).dump());
        return 0;
    } catch (const std::exception & e) { return fail(output, e.what()); }
      catch (...) { return fail(output, "unknown native request error"); }
}
void coegent_llama_close(void * handle) noexcept { delete static_cast<engine *>(handle); }
}
