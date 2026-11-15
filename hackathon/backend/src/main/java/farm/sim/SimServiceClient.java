package farm.sim;

import java.nio.charset.StandardCharsets;
import java.time.Duration;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestClient;

/**
 * Thin HTTP client for the Python fleet simulator service.
 *
 * <p>The simulator is the single source of truth: this backend never keeps its own copy of
 * tractors, jobs or metrics. JSON bodies are passed through unchanged so the frontend always
 * sees exactly what the simulator reported.
 */
@Component
public class SimServiceClient {

    /** Status + raw JSON body returned by the simulator service. */
    public record SimResponse(int status, String body) {}

    private final RestClient client;
    private final String baseUrl;

    public SimServiceClient(
            @Value("${sim.service.url}") String baseUrl,
            @Value("${sim.service.connect-timeout:3s}") Duration connectTimeout,
            @Value("${sim.service.read-timeout:180s}") Duration readTimeout) {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(connectTimeout);
        factory.setReadTimeout(readTimeout);
        this.baseUrl = baseUrl.replaceAll("/+$", "");
        this.client = RestClient.builder().baseUrl(this.baseUrl).requestFactory(factory).build();
    }

    public String baseUrl() {
        return baseUrl;
    }

    public SimResponse get(String pathAndQuery) {
        return call(HttpMethod.GET, pathAndQuery, null);
    }

    public SimResponse post(String path, String jsonBody) {
        return call(HttpMethod.POST, path, jsonBody == null || jsonBody.isBlank() ? "{}" : jsonBody);
    }

    private SimResponse call(HttpMethod method, String uri, String body) {
        try {
            RestClient.RequestBodySpec spec = client.method(method).uri(uri).accept(MediaType.APPLICATION_JSON);
            if (body != null) {
                spec = spec.contentType(MediaType.APPLICATION_JSON).body(body);
            }
            return spec.exchange((req, res) -> new SimResponse(
                    res.getStatusCode().value(),
                    new String(res.getBody().readAllBytes(), StandardCharsets.UTF_8)));
        } catch (ResourceAccessException e) {
            throw new SimUnavailableException(baseUrl, e);
        }
    }

    /** Raised when the simulator service cannot be reached. */
    public static class SimUnavailableException extends RuntimeException {
        public SimUnavailableException(String url, Throwable cause) {
            super("Fleet simulator service unreachable at " + url, cause);
        }
    }

    /** Small helper so callers can build JSON error bodies without a JSON library. */
    public static String errorJson(String message) {
        String escaped = message.replace("\\", "\\\\").replace("\"", "\\\"");
        return "{\"error\":\"" + escaped + "\"}";
    }
}
